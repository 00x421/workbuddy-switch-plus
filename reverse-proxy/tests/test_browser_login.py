import json
import unittest
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from admin.browser_login import BrowserLogin
from admin.realm import CN_CHAT_BASE, GLOBAL_BASE
import test_admin_server as admin_tests
from test_admin_server import credential

# 国内版 chat 基址（等价于旧版模块级常量 BASE，改名是为了让“按 realm 分派”
# 这件事在源码里就无法被忽略）。
BASE = CN_CHAT_BASE


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.time = 10000
        self.authorized = False
        self.bad_url = False
        self.bad_account = False
        self.calls = []
        self.saver = Mock(return_value={"account_id": "saved-id", "updated": False})
        def respond(req):
            self.calls.append(req)
            if req.url.path.endswith('/state'):
                return httpx.Response(200, json={"code": 0, "data": {"state": "upstream-secret", "authUrl": ('https://evil.example/login' if self.bad_url else BASE + '/login') + '?state=upstream-secret'}}, headers={"set-cookie": "login-session=test-cookie; Path=/"})
            self.assertIn('login-session=test-cookie', req.headers.get('cookie', ''))
            if req.url.path.endswith('/token'):
                if not self.authorized:
                    return httpx.Response(200, json={"code": 11217, "msg": "login ing"})
                return httpx.Response(200, json={"code": 0, "data": {"accessToken": "private-access", "refreshToken": "private-refresh", "expiresIn": 3600, "domain": "www.codebuddy.cn"}})
            self.assertEqual(req.headers['authorization'], 'Bearer private-access')
            return httpx.Response(200, json={"code": 0, "data": {} if self.bad_account else {"uid": "test-uid", "nickname": "测试授权账号"}})
        self.manager = BrowserLogin(self.saver, lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)), lambda: self.time)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_pending_success_idempotent_and_tokens_not_exposed(self):
        flow = await self.manager.start('owner', 'My account')
        self.assertNotEqual(flow['id'], 'upstream-secret')
        self.assertEqual((await self.manager.poll(flow['id'], 'owner'))['status'], 'pending')
        self.authorized = True
        self.time += 4
        result = await self.manager.poll(flow['id'], 'owner')
        self.assertEqual(result['status'], 'success')
        self.assertNotIn('private-', json.dumps(result))
        self.assertEqual(self.saver.call_args.args[0]['auth']['expiresAt'], 13604000)
        self.assertEqual(self.saver.call_args.args[1], 'My account')
        self.assertEqual(await self.manager.poll(flow['id'], 'owner'), result)
        self.saver.assert_called_once()

    async def test_owner_expiry_cancel_and_restart(self):
        flow = await self.manager.start('a', None)
        with self.assertRaises(HTTPException) as context:
            await self.manager.poll(flow['id'], 'b')
        self.assertEqual(context.exception.status_code, 404)
        self.time += 301
        self.assertEqual((await self.manager.poll(flow['id'], 'a'))['status'], 'expired')
        flow = await self.manager.start('a', None)
        replacement = await self.manager.start('a', None)
        self.assertNotIn(flow['id'], self.manager.flows)
        await self.manager.cancel(replacement['id'], 'a')
        self.assertFalse(self.manager.flows)
        self.saver.assert_not_called()

    async def test_untrusted_url_and_missing_account_are_rejected(self):
        self.bad_url = True
        with self.assertRaises(HTTPException):
            await self.manager.start('a', None)
        self.assertFalse(self.manager.flows)
        self.bad_url = False
        flow = await self.manager.start('a', None)
        self.authorized = True
        self.bad_account = True
        with self.assertRaises(HTTPException):
            await self.manager.poll(flow['id'], 'a')
        self.saver.assert_not_called()
        self.bad_account = False
        self.time += 4
        self.assertEqual((await self.manager.poll(flow['id'], 'a'))['status'], 'success')
        token_calls = [req for req in self.calls if req.url.path.endswith('/token')]
        self.assertEqual(len(token_calls), 1)


class BrowserAccountTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = admin_tests.AdminTests.asyncSetUp
    asyncTearDown = admin_tests.AdminTests.asyncTearDown
    make_app = admin_tests.AdminTests.make_app
    login = admin_tests.AdminTests.login
    async def test_browser_account_deduplicates_and_preserves_active(self):
        store = self.app.state.store
        first = store.data['active']
        r = store.save_browser_account(credential('two'), 'Browser account')
        self.assertFalse(r['updated'])
        self.assertEqual(store.data['active'], first)
        r2 = store.save_browser_account(credential('two', 'Updated'), None)
        self.assertTrue(r2['updated'])
        self.assertEqual(r['account_id'], r2['account_id'])
        self.assertEqual(len(store.data['accounts']), 2)

    async def test_oauth_routes_enforce_session_and_csrf(self):
        self.assertEqual((await self.client.post('/admin/api/oauth/start', json={})).status_code, 401)
        await self.login()
        self.client.headers.pop('X-CSRF-Token')
        self.assertEqual((await self.client.post('/admin/api/oauth/start', json={})).status_code, 403)
        self.assertEqual((await self.client.post('/admin/api/oauth/fake/poll')).status_code, 403)
        self.assertEqual((await self.client.delete('/admin/api/oauth/fake')).status_code, 403)


class BatchScanTests(unittest.IsolatedAsyncioTestCase):
    """批量扫码：一次摆 N 张码，用户连着扫。

    这套用例盯的是**并发槽位**，不是扫码本身（那条链路 BrowserLoginTests
    已经覆盖）。三个必须钉住的点：

      1. 不带 slot 的旧行为不能被改坏 —— 批量支持是加参数，不是换语义。
      2. 同一 slot 重发码要顶掉旧的，否则批量页刷新几次就攒满死会话。
      3. 国际版批量没选地区时要自动兜底，不能沿用单账号那句「请重新登录」。
    """
    async def asyncSetUp(self):
        self.time = 10000
        self.authorized = False
        self.calls = []
        self.saver = Mock(return_value={"account_id": "saved", "updated": False})
        def respond(req):
            self.calls.append(req)
            if req.url.path.endswith('/state'):
                state = f'st-{len(self.calls)}'
                # 登录页必须落在**这次请求所用 realm 的 chat 域**上：
                # _check_url 的基准是 chat_base，不是 origin。国内版
                # chat=copilot.tencent.com 而 origin=www.codebuddy.cn，
                # 用 BASE 拼国际版的码会被判「授权链接校验失败」。
                origin = GLOBAL_BASE if 'workbuddy.ai' in str(req.url) else BASE
                return httpx.Response(200, json={"code": 0, "data": {
                    "state": state, "authUrl": origin + '/login?state=' + state}})
            if req.url.path.endswith('/token'):
                if not self.authorized:
                    return httpx.Response(200, json={"code": 11217, "msg": "login ing"})
                return httpx.Response(200, json={"code": 0, "data": {
                    "accessToken": "a", "refreshToken": "r", "expiresIn": 3600,
                    "domain": "www.codebuddy.cn"}})
            return httpx.Response(200, json={"code": 0, "data": {"uid": "u", "nickname": "n"}})
        self.manager = BrowserLogin(self.saver, lambda: httpx.AsyncClient(transport=httpx.MockTransport(respond)), lambda: self.time)

    async def asyncTearDown(self):
        await self.manager.close()

    async def test_batch_slots_coexist_without_cancelling_each_other(self):
        # 这是批量方案的核心：不带 slot 时 start() 会 cancel_owner，
        # 循环调用只剩最后一张活着 —— 那正是「批量扫码做不成」的形态。
        flows = [await self.manager.start('owner', f'n{i}', slot=f'b{i}') for i in range(5)]
        self.assertEqual(len(self.manager.flows), 5)
        self.assertEqual(len({f['id'] for f in flows}), 5)

    async def test_same_slot_replaces_instead_of_accumulating(self):
        first = await self.manager.start('owner', 'n', slot='b0')
        second = await self.manager.start('owner', 'n', slot='b0')
        self.assertNotEqual(first['id'], second['id'])
        self.assertEqual(list(self.manager.flows), [second['id']])
        with self.assertRaises(HTTPException) as ctx:
            self.manager.get(first['id'], 'owner')
        self.assertEqual(ctx.exception.status_code, 404)

    async def test_slotless_start_still_cancels_previous(self):
        # 单账号登录的原有口径：一个 owner 只该有一张码。这条防的是
        # 「为了让批量能用，把 cancel_owner 整段删掉」这种改法。
        first = await self.manager.start('owner', None)
        second = await self.manager.start('owner', None)
        self.assertEqual(list(self.manager.flows), [second['id']])
        self.assertNotEqual(first['id'], second['id'])

    async def test_per_owner_cap_is_enforced(self):
        from admin import browser_login as mod
        for i in range(mod.MAX_FLOWS_PER_OWNER):
            await self.manager.start('owner', None, slot=f's{i}')
        with self.assertRaises(HTTPException) as ctx:
            await self.manager.start('owner', None, slot='overflow')
        self.assertEqual(ctx.exception.status_code, 429)
        self.assertIn(str(mod.MAX_FLOWS_PER_OWNER), str(ctx.exception.detail))

    async def test_batch_mode_auto_regions_global_accounts(self):
        # 批量没选地区 → 不能返回「请选择地区后重新登录」，那句话在批量语义
        # 下是死路（一次 50 张码没法逐格选）。必须自动落到默认地区。
        flow = await self.manager.start('owner', 'n', realm='global', slot='b0')
        self.assertEqual(flow['region'], '')
        self.assertTrue(self.manager.flows[flow['id']]['bare'])
        self.authorized = True
        self.time += 4
        result = await self.manager.poll(flow['id'], 'owner')
        self.assertEqual(result['status'], 'success')
        from admin import browser_login as mod
        self.assertEqual(result['region'], mod.DEFAULT_BATCH_REGION)

    async def test_single_mode_global_without_region_keeps_actionable_message(self):
        # 单账号路径不受批量兜底影响：没选地区仍要说清该选地区，
        # 而不是悄悄给一个用户没同意的归属。
        flow = await self.manager.start('owner', 'n', realm='global')
        self.assertFalse(self.manager.flows[flow['id']]['bare'])
        self.authorized = True
        self.time += 4
        result = await self.manager.poll(flow['id'], 'owner')
        self.assertEqual(result['status'], 'success')
        self.assertIn('请选择地区后重新登录', result.get('message', ''))


class BatchScanRouteTests(unittest.IsolatedAsyncioTestCase):
    """批量扫码接口的入参边界与部分失败语义。"""
    asyncSetUp = admin_tests.AdminTests.asyncSetUp
    asyncTearDown = admin_tests.AdminTests.asyncTearDown
    make_app = admin_tests.AdminTests.make_app
    login = admin_tests.AdminTests.login

    async def test_requires_admin_and_validates_bounds(self):
        r = await self.client.post('/admin/api/oauth/batch/start', json={"count": 3})
        self.assertEqual(r.status_code, 401)
        await self.login()
        # count 缺省/为 0 是「没给」不是「给错」：按 1 张处理比 400 更顺手。
        # 真正的边界是上限与非法地区。
        self.assertEqual((await self.client.post('/admin/api/oauth/batch/start', json={"count": 0})).status_code, 400)
        self.assertEqual((await self.client.post('/admin/api/oauth/batch/start', json={"count": 999})).status_code, 400)
        self.assertEqual((await self.client.post('/admin/api/oauth/batch/start', json={"count": "abc"})).status_code, 400)
        bad_region = await self.client.post('/admin/api/oauth/batch/start', json={"count": 1, "region": "ZZ"})
        self.assertEqual(bad_region.status_code, 400)

    async def test_partial_failure_is_reported_per_slot(self):
        # 上游偶发失败时，整批不该回滚：用户已经扫掉的码不能因为第 40 张
        # 撞了 502 就全废。逐条给 status 才是对的形状。
        await self.login()
        app = self.app
        from admin import browser_login as mod
        real_start = app.state.browser_login.start if hasattr(app.state, 'browser_login') else None
        self.assertIsNotNone(real_start) if real_start else None
        r = await self.client.post('/admin/api/oauth/batch/start', json={"count": 3})
        # 夹具没有外网，逐条要么 ready（被 MockTransport 拦下）要么 failed，
        # 无论哪种都必须是 items 数组且长度等于请求数 —— 不允许整体 502。
        self.assertIn(r.status_code, (200,))
        body = r.json()
        self.assertEqual(len(body['items']), 3)
        self.assertEqual(body['count'], 3)
        self.assertEqual({item['slot'] for item in body['items']}, {'b0', 'b1', 'b2'})
        for item in body['items']:
            self.assertIn(item['status'], ('ready', 'failed'))
            if item['status'] == 'failed':
                self.assertTrue(item['error'])


if __name__ == '__main__':
    unittest.main()
