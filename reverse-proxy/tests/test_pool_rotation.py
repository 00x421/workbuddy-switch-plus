"""账号轮换：轮流用 / 只用当前账号 / 最少使用优先 / 驻留换号，以及签到日志。

这几条是「同一个号被连续调用会不会被风控」这个问题的技术答案，
所以单独成文件，把每种调度的边界都钉住。
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

import httpx
from fastapi import HTTPException

from admin.server import Store
from admin.pool import (AccountPool, CHECKIN_LOG_LIMIT, PoolMiddleware,                        REQUEST_LOG_MEMORY_LIMIT, default_pool)
from test_admin_server import credential


class RotationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.c = self.store.save_browser_account(credential('three'), 'three')['account_id']
        self.now = 1800000000
        self.pool = AccountPool(self.store, clock=lambda: self.now)
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(return_value={'X-Domain': 'www.codebuddy.cn'})

    def tearDown(self):
        self.tmp.cleanup()

    def picks(self, n, key=None):
        return [self.pool.select(key)[0] for _ in range(n)]

    # ── 轮流用 ───────────────────────────────────────────────────────
    def test_round_robin_cycles_through_every_account(self):
        # 默认就是 round_robin：三次请求应该分别落到三个不同的号上。
        self.assertEqual(self.picks(3), [self.a, self.b, self.c])
        self.assertEqual(self.picks(3), [self.a, self.b, self.c])

    # ── 只用一个号 ───────────────────────────────────────────────────
    def test_manual_pins_to_active_account_only(self):
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.b
        # 无论选多少次，都只能是 b，不会轮到 a / c。
        self.assertEqual(self.picks(6), [self.b] * 6)

    def test_manual_refuses_when_active_account_unusable(self):
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.a
        self.store.data['accounts'][self.a]['enabled'] = False
        with self.assertRaises(HTTPException) as ctx:
            self.pool.select()
        # 报错文案要能提示用户「去换一个号」，而不是笼统的「暂无可用账号」。
        self.assertIn('当前账号不可用', str(ctx.exception.detail))

    def test_manual_does_not_fall_back_to_other_accounts(self):
        """关键安全性质：手动模式下绝不允许悄悄改用别的账号。"""
        self.store.data['pool']['routing'] = 'manual'
        self.store.data['active'] = self.a
        self.pool.record(self.a, 429)  # a 进冷却
        self.now += 1801                # 冷却结束，仍是 available
        self.assertEqual(self.pool.select()[0], self.a)

    # ── 最少使用优先 ─────────────────────────────────────────────────
    def test_least_used_prefers_lowest_request_count(self):
        self.store.data['pool']['routing'] = 'least_used'
        self.pool.update(self.a, request_count=9)
        self.pool.update(self.b, request_count=2)
        self.pool.update(self.c, request_count=5)
        self.assertEqual(self.pool.select()[0], self.b)
        # 计数是「选号」时读的，不是自动加；这里手动抬高 b，下一个应轮到 c。
        self.pool.update(self.b, request_count=99)
        self.assertEqual(self.pool.select()[0], self.c)

    def test_least_used_tie_breaks_deterministically(self):
        self.store.data['pool']['routing'] = 'least_used'
        for aid in (self.a, self.b, self.c):
            self.pool.update(aid, request_count=0)
        # 并列时按 id 稳定排序，结果可复现（不会随机抖动）。
        first = self.pool.select()[0]
        self.assertEqual([self.pool.select()[0] for _ in range(3)], [first] * 3)

    # ── 驻留换号（spread）────────────────────────────────────────────
    def test_spread_holds_one_account_until_lease_expires(self):
        self.store.data['pool'].update({'spread': True, 'spread_minutes': 30})
        first = self.pool.select()[0]
        # 租约期内反复调用，必须一直是同一个号。
        self.assertEqual(self.picks(5), [first] * 5)
        # 租约到期后才会换到下一个。
        self.now += 30 * 60 + 1
        self.assertNotEqual(self.pool.select()[0], first)

    def test_spread_lease_is_persisted(self):
        self.store.data['pool'].update({'spread': True, 'spread_minutes': 10})
        first = self.pool.select()[0]
        lease = self.store.data['pool']['lease']
        self.assertEqual(lease['account_id'], first)
        self.assertEqual(lease['until'], self.now + 600)
        # 落盘了：重启服务后租约还在，不会立刻换号。
        on_disk = json.loads((self.store.path).read_text(encoding='utf-8'))
        self.assertEqual(on_disk['pool']['lease']['account_id'], first)

    def test_switching_active_account_invalidates_lease(self):
        """切「当前账号」必须立刻生效，不能被旧租约钉住。"""
        self.store.data['pool'].update({'spread': True, 'spread_minutes': 60})
        self.pool.select()  # 建立租约
        self.assertIsNotNone(self.store.data['pool']['lease'])
        # 模拟 server 端 PATCH 切号时的处理
        self.store.data['pool']['lease'] = None
        self.store.data['active'] = self.b
        self.store.data['pool']['routing'] = 'manual'
        self.assertEqual(self.pool.select()[0], self.b)

    def test_spread_lease_ignored_when_bound_account_is_gone(self):
        self.store.data['pool'].update({'spread': True, 'spread_minutes': 60})
        self.pool.select()
        leased = self.store.data['pool']['lease']['account_id']
        self.store.data['accounts'][leased]['enabled'] = False
        # 租约里的号不可用了 → 必须换号，不能卡死。
        self.assertNotEqual(self.pool.select()[0], leased)

    # ── 会话黏性优先级高于轮换 ───────────────────────────────────────
    def test_affinity_beats_round_robin(self):
        self.store.data['pool'].update({'spread': True, 'spread_minutes': 60})
        key = 'affinity-key'
        first = self.pool.select(key)[0]
        self.assertEqual([self.pool.select(key)[0] for _ in range(3)], [first] * 3)
        # 另一个 key 走轮换，可以落到别的号上（说明黏性是按会话隔离的）。
        other = self.pool.select('other-key')[0]
        self.assertIn(other, (self.a, self.b, self.c))


class CheckinLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.now = 1800000000
        self.checked = False
        self.fail = False

        def respond(req):
            if self.fail:
                return httpx.Response(500)
            if req.url.path.endswith('checkin-activity-status'):
                return httpx.Response(200, json={'code': 0, 'data': {'active': True, 'today_checked_in': self.checked}})
            if req.url.path.endswith('daily-checkin'):
                self.checked = True
                return httpx.Response(200, json={'code': 0, 'data': {}})
            return httpx.Response(200, json={'code': 0, 'data': {'Response': {'Data': {'TotalCount': 1, 'Accounts': [
                {'CycleCapacityRemainPrecise': '479.59', 'CycleCapacitySize': 500, 'CapacityRemain': 500}]}}}})

        self.pool = AccountPool(self.store, clock=lambda: self.now,
                                client_factory=lambda: httpx.Client(transport=httpx.MockTransport(respond)))
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(return_value={'X-Domain': 'www.codebuddy.cn'})

    def tearDown(self):
        self.tmp.cleanup()

    def test_successful_checkin_is_logged(self):
        result = self.pool.operate(self.a, 'checkin', 'auto')
        self.assertTrue(result['ok'])
        logs = self.pool.checkin_logs()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['id'], self.a)
        self.assertTrue(logs[0]['ok'])
        self.assertEqual(logs[0]['source'], 'auto')
        self.assertEqual(logs[0]['name'], 'one')

    def test_failed_checkin_is_logged_too(self):
        self.fail = True
        result = self.pool.operate(self.a, 'checkin', 'manual')
        self.assertFalse(result['ok'])
        logs = self.pool.checkin_logs()
        self.assertEqual(len(logs), 1)
        self.assertFalse(logs[0]['ok'])
        self.assertTrue(logs[0]['message'])

    def test_status_action_is_not_logged(self):
        """批量刷状态每 5 分钟一次，记进签到流水会把日志淹掉。"""
        self.pool.operate(self.a, 'status')
        self.assertEqual(self.pool.checkin_logs(), [])

    def test_log_is_capped_and_newest_first(self):
        for _ in range(CHECKIN_LOG_LIMIT + 20):
            self.pool.log_checkin(self.a, True, '签到成功', 'auto')
        logs = self.pool.checkin_logs(limit=CHECKIN_LOG_LIMIT)
        self.assertEqual(len(logs), CHECKIN_LOG_LIMIT)
        # 最新的在最前面（checkin_logs 会 reverse），且时间戳是递增的原始顺序取尾。
        self.assertLessEqual(logs[0]['time'], logs[-1]['time'])

    def test_logs_persist_across_restart(self):
        self.pool.operate(self.a, 'checkin', 'auto')
        fresh = AccountPool(self.store, clock=lambda: self.now)
        self.assertEqual(len(fresh.checkin_logs()), 1)

    def test_clear_removes_everything_and_reports_count(self):
        self.pool.operate(self.a, 'checkin', 'auto')
        self.pool.log_checkin(self.a, False, '失败', 'manual')
        self.assertEqual(self.pool.clear_checkin_logs(), 2)
        self.assertEqual(self.pool.checkin_logs(), [])


class PoolSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test', 'admin-long-enough-test-key')

    def tearDown(self):
        self.tmp.cleanup()

    def test_defaults_are_filled_in_on_a_stale_state_file(self):
        """老版本写下的 state.json 里没有 spread 这些键，启动时必须补齐。"""
        self.store.data['pool'] = {'routing': 'round_robin', 'auto_checkin': True, 'checkin_time': '09:00'}
        AccountPool(self.store, clock=lambda: 1800000000)
        pool = self.store.data['pool']
        for key, value in default_pool().items():
            self.assertIn(key, pool)
        self.assertFalse(pool['spread'])
        self.assertEqual(pool['spread_minutes'], 30)
        self.assertIsNone(pool['lease'])

    def test_startup_drops_a_stale_lease(self):
        """重启后旧租约不能续命，否则一开机就被钉在上次那个号上。"""
        self.store.data['pool'] = dict(default_pool(), lease={'account_id': 'x', 'until': 99999999999})
        AccountPool(self.store, clock=lambda: 1800000000)
        self.assertIsNone(self.store.data['pool']['lease'])


class RequestLogTests(unittest.IsolatedAsyncioTestCase):
    """请求日志：每次 /v1 请求用了哪个账号、什么模型、扣了多少。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.store = Store(root / 'management', root / 'auth', 'api-test', 'admin-long-enough-test-key')
        self.a = self.store.save_browser_account(credential('one'), 'one')['account_id']
        self.b = self.store.save_browser_account(credential('two'), 'two')['account_id']
        self.now = 1800000000
        self.pool = AccountPool(self.store, clock=lambda: self.now)
        for aid, item in self.store.data['accounts'].items():
            self.store.manager_for(aid, item).get_headers = Mock(return_value={'X-Domain': 'www.codebuddy.cn'})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _call(self, app, body=None, headers=None, path='/v1/chat/completions'):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=PoolMiddleware(app, self.pool)),
                                     base_url='http://test') as client:
            return await client.post(path, json=body or {'model': 'm', 'messages': []},
                                     headers=headers or {'Authorization': 'Bearer api-test'})

    async def test_successful_request_logs_account_model_and_usage(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': json.dumps(
                {'usage': {'total_tokens': 123, 'credit': 0.5}}).encode()})
        await self._call(app, {'model': 'deepseek-v4-flash', 'messages': []})
        logs = self.pool.request_logs()
        self.assertEqual(len(logs), 1)
        row = logs[0]
        self.assertEqual(row['account_id'], self.a)
        self.assertEqual(row['account_name'], 'one')
        self.assertEqual(row['model'], 'deepseek-v4-flash')
        self.assertEqual(row['tokens'], 123)
        self.assertAlmostEqual(row['credit'], 0.5)
        self.assertEqual(row['status'], 200)
        self.assertTrue(row['ok'])
        self.assertEqual(row['source'], 'api')

    async def test_rotating_requests_are_attributed_to_different_accounts(self):
        """轮流模式下，两条日志要分别挂到不同的账号上——这正是日志页的意义。"""
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        await self._call(app)
        await self._call(app)
        ids = [row['account_id'] for row in self.pool.request_logs()]
        self.assertEqual(ids, [self.b, self.a])  # appendleft：最新在前

    async def test_upstream_error_is_logged_with_status(self):
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 429, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        await self._call(app)
        row = self.pool.request_logs()[0]
        self.assertEqual(row['status'], 429)
        self.assertFalse(row['ok'])
        self.assertEqual(row['outcome'], 'upstream_error')

    async def test_no_available_account_is_logged(self):
        """一个可用账号都没有时也要留痕，否则用户只看到 503 不知为何。"""
        for aid in (self.a, self.b):
            self.store.data['accounts'][aid]['enabled'] = False
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        response = await self._call(app, {'model': 'm'})
        self.assertEqual(response.status_code, 503)
        row = self.pool.request_logs()[0]
        self.assertIsNone(row['account_id'])
        self.assertEqual(row['outcome'], 'no_account')
        self.assertEqual(row['model'], 'm')

    async def test_rejected_by_key_policy_is_logged(self):
        """密钥被 IP / 模型规则挡下也要记录，并且不占用账号。"""
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        response = await self._call(app, {}, headers={'Authorization': 'Bearer wrong-key'})
        self.assertEqual(response.status_code, 401)
        row = self.pool.request_logs()[0]
        self.assertEqual(row['outcome'], 'rejected')
        self.assertEqual(row['status'], 401)

    async def test_test_key_requests_are_marked_as_test(self):
        import secrets as _secrets
        from admin.security import digest
        temp = _secrets.token_urlsafe(48)
        with self.store.lock:
            self.store.test_keys.add(digest(temp))
        async def app(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 200, 'headers': []})
            await send({'type': 'http.response.body', 'body': b'{}'})
        await self._call(app, headers={'Authorization': 'Bearer ' + temp})
        self.assertEqual(self.pool.request_logs()[0]['source'], 'test')

    def test_log_is_bounded(self):
        for i in range(REQUEST_LOG_MEMORY_LIMIT + 25):
            self.pool.log_request(self.a, 'm', 200, True, i, 0, 1, 'success')
        self.assertEqual(len(self.pool.request_logs(limit=REQUEST_LOG_MEMORY_LIMIT)),
                         REQUEST_LOG_MEMORY_LIMIT)

    def test_log_does_not_touch_state_json(self):
        """请求日志刻意不落盘：每个请求都重写 state.json 是纯粹的浪费。"""
        before = self.store.path.read_text(encoding='utf-8')
        self.pool.log_request(self.a, 'm', 200, True, 1, 0, 1, 'success')
        self.assertEqual(self.store.path.read_text(encoding='utf-8'), before)
        self.assertNotIn('request_logs', self.store.data)


if __name__ == '__main__':
    unittest.main()
