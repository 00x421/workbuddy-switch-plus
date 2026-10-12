/**
 * 「退出登录」的策略：清什么、置什么标记、通知谁。
 *
 * 抽成纯函数是为了能直接单测 —— 退出逻辑的正确性不该靠「跑起来点一下」来保证，
 * 它有两个容易写错的硬要求：
 *
 *   1. 幂等：服务端可达 / 不可达 / 已经登出，点退出都要回到登录页；
 *   2. 停得住：回到登录页之后不能又被自动登回去。
 *
 * ## 第 2 条是「点退出还是退不出去」的真正原因
 *
 * 桌面端把「保存密码」写进 `~/.wb-switch-plus/reverse-proxy-login.json`，
 * 登录卡挂载时会读它并自动提交登录。于是：
 *
 *   点退出 → 只清了内存里的 token → 切回登录页 → 登录卡读回保存的口令
 *          → 立刻自动登录成功 → 又回到控制台
 *
 * 用户看到的就是「点了退出没反应」——看起来像退出失败，其实是退出成功后又马上登回来了。
 * 那份文件里存着明文口令。
 *
 * 所以退出必须同时：清内存 token、删掉保存的凭据、立「本次会话已显式退出」标记。
 * 标记放模块作用域（不是组件 state）：桌面端切页签会重新挂载页面，
 * 组件 state 会被重置，标记一丢就又能自动登录，退出只生效一次。
 */

/** 退出时按序执行的动作。抽出来是为了让「顺序」本身可被断言。 */
export type SignOutEffects = {
  /** 清内存登录态（token / 缓存身份 / 概览）。 */
  clearLocal: () => void;
  /** 置「本次会话已显式退出」标记，让登录卡不再自动登录。 */
  blockAutoLogin: () => void;
  /** 删除保存的凭据。不删就会被登录卡读回来自动登录。 */
  clearSavedLogin: () => Promise<unknown>;
  /** 通知服务端吊销会话。 */
  notifyServer: () => Promise<unknown>;
};

/**
 * 「本次会话已显式退出」标记。
 *
 * 模块作用域、进程生命周期：
 * · 故意不是组件 state —— 切页签重挂载会把 state 重置，标记一丢，
 *   登录卡就又能自动登录，表现为「退出只生效一次」；
 * · 关掉软件重开时它自然回到 false，「打开就自动登录」的原有便利不受影响。
 */
let autoLoginBlocked = false;

/** 登录卡该不该读保存的凭据并自动登录。 */
export function shouldAutoLogin(): boolean {
  return !autoLoginBlocked;
}

/** 只给测试用：把标记复位，避免用例之间互相污染。 */
export function resetAutoLoginBlock(): void {
  autoLoginBlocked = false;
}

/**
 * 执行退出。
 *
 * 顺序是加载项，不是实现细节：
 *
 *   1. `clearLocal` —— 同步，界面立刻回到登录页。这是「点退出必须有反应」的保证；
 *   2. `blockAutoLogin` —— 同步，且在清凭据之前。
 *      先立标记再看清凭据的结果：清凭据可能失败（文件被占用）或很慢，
 *      若反过来，那段窗口里登录卡是「可以自动登录」的，用户还是会被登回去；
 *   3. `clearSavedLogin` —— 异步且不 await：
 *   4. `notifyServer` —— 异步且不 await。
 *
 * 3 / 4 都不 await：桌面端的目标是「立刻回到登录页」，
 * 不能因为一次网络请求超时、服务没起来、或文件 I/O 卡住而卡住退出。
 * 两者的失败都被吞掉 —— 本地已经退干净了，服务端那次吊销是尽力而为
 * （而且 logout 本身是幂等的，没登录也回 200）。
 *
 * 返回一个「都收尾完了」的 Promise，仅为让测试能等到异步副作用结束；
 * 调用方不需要 await 它。
 */
export function performSignOut(effects: SignOutEffects): Promise<void> {
  // 1) 同步清本地 → 界面立刻回到登录页。
  effects.clearLocal();
  // 2) 同步立标记 → 登录卡从此不再自动登录。
  autoLoginBlocked = true;
  effects.blockAutoLogin();
  // 3) / 4) 异步收尾，失败一律吞掉。
  return Promise.all([
    Promise.resolve()
      .then(() => effects.clearSavedLogin())
      .catch(() => undefined),
    Promise.resolve()
      .then(() => effects.notifyServer())
      .catch(() => undefined),
  ]).then(() => undefined);
}