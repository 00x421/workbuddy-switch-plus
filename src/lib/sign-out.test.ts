import { afterEach, describe, expect, it, vi } from "vitest";

import { performSignOut, resetAutoLoginBlock, shouldAutoLogin, type SignOutEffects } from "./sign-out";

/**
 * 退出登录的两条硬要求：幂等、停得住。
 *
 * 这两条都来自同一个真实故障：用户报「点退出还是退不出去」。
 * 原因不是退出没执行，而是执行完之后又被自动登回来了 ——
 * 桌面端把「保存密码」写进了 `~/.wb-switch-plus/reverse-proxy-login.json`，
 * 登录卡挂载时会读它并自动提交登录。所以「只清内存 token」的退出，
 * 表现上就是「点了没反应」。
 *
 * 因此下面既测「动作做全了没有」，也测「顺序对不对」——
 * 立标记必须早于清凭据，否则两者之间那段窗口里登录卡仍会自动登录。
 */

function effects(overrides: Partial<SignOutEffects> = {}): SignOutEffects {
  return {
    clearLocal: vi.fn(),
    blockAutoLogin: vi.fn(),
    clearSavedLogin: vi.fn(() => Promise.resolve()),
    notifyServer: vi.fn(() => Promise.resolve()),
    ...overrides,
  };
}

afterEach(() => {
  resetAutoLoginBlock();
  vi.restoreAllMocks();
});

describe("退出登录", () => {
  it("清内存登录态、清保存的凭据、通知服务端 —— 三件事都要做", async () => {
    const e = effects();

    await performSignOut(e);

    expect(e.clearLocal).toHaveBeenCalledTimes(1);
    // 不清保存的凭据，登录卡会把用户自动登回来。
    expect(e.clearSavedLogin).toHaveBeenCalledTimes(1);
    expect(e.notifyServer).toHaveBeenCalledTimes(1);
  });

  it("清本地是同步先做的 —— 界面不能等服务端才回到登录页", async () => {
    const order: string[] = [];
    const e = effects({
      clearLocal: vi.fn(() => { order.push("clearLocal"); }),
      clearSavedLogin: vi.fn(() => { order.push("clearSavedLogin"); return Promise.resolve(); }),
      notifyServer: vi.fn(() => { order.push("notifyServer"); return Promise.resolve(); }),
    });

    const pending = performSignOut(e);
    // 还没 await：同步那一步必须已经做完了。
    expect(order).toEqual(["clearLocal"]);
    await pending;
    expect(order[0]).toBe("clearLocal");
  });

  it("禁止自动登录的标记在**清凭据之前**就立好了", async () => {
    // clearSavedLogin 故意卡住，模拟文件被占用 / I/O 慢。
    let release: () => void = () => {};
    const blocked = new Promise<void>((resolve) => { release = resolve; });
    const e = effects({
      clearSavedLogin: vi.fn(() => blocked),
    });

    const pending = performSignOut(e);

    // 清凭据还没完成，但自动登录已经被挡住。
    // 若顺序反过来（先清凭据再立标记），这段窗口里登录卡就会把用户登回去。
    expect(shouldAutoLogin()).toBe(false);

    release();
    await pending;
    expect(shouldAutoLogin()).toBe(false);
  });

  it("服务端不可达（logout 抛错）仍然退得出去，且不往外抛", async () => {
    const e = effects({
      notifyServer: vi.fn(() => Promise.reject(new Error("无法连接服务端"))),
    });

    await expect(performSignOut(e)).resolves.toBeUndefined();
    expect(e.clearLocal).toHaveBeenCalledTimes(1);
    expect(e.clearSavedLogin).toHaveBeenCalledTimes(1);
    expect(shouldAutoLogin()).toBe(false);
  });

  it("清保存的凭据失败（文件被占用）也仍然退得出去", async () => {
    const e = effects({
      clearSavedLogin: vi.fn(() => Promise.reject(new Error("EBUSY"))),
    });

    await expect(performSignOut(e)).resolves.toBeUndefined();
    // 关键：凭据没删掉，内存标记照样挡住了自动登录。
    expect(shouldAutoLogin()).toBe(false);
  });

  it("两个异步收尾同时失败也不炸", async () => {
    const e = effects({
      clearSavedLogin: vi.fn(() => Promise.reject(new Error("boom1"))),
      notifyServer: vi.fn(() => Promise.reject(new Error("boom2"))),
    });

    await expect(performSignOut(e)).resolves.toBeUndefined();
    expect(shouldAutoLogin()).toBe(false);
  });

  it("幂等：连点两次退出不抛错，动作各做一次", async () => {
    const e = effects();

    await performSignOut(e);
    await performSignOut(e);

    expect(e.clearLocal).toHaveBeenCalledTimes(2);
    expect(e.clearSavedLogin).toHaveBeenCalledTimes(2);
    expect(shouldAutoLogin()).toBe(false);
  });

  it("「已经登出」（服务端返回成功但没有会话）也算正常退出", async () => {
    const e = effects({ notifyServer: vi.fn(() => Promise.resolve({ ok: true })) });

    await performSignOut(e);

    expect(shouldAutoLogin()).toBe(false);
  });
});

describe("自动登录开关", () => {
  it("初始允许自动登录（「打开软件自动登录」的原有便利）", () => {
    resetAutoLoginBlock();
    expect(shouldAutoLogin()).toBe(true);
  });

  it("退出之后不再自动登录，且**不会**因为再次判定而复位", async () => {
    resetAutoLoginBlock();
    await performSignOut(effects());

    // 多问几次都要是 false —— 组件重挂载会反复调它。
    expect(shouldAutoLogin()).toBe(false);
    expect(shouldAutoLogin()).toBe(false);
  });

  it("重置后恢复允许 —— 用来验证标记确实只在「显式退出」时置位", async () => {
    await performSignOut(effects());
    expect(shouldAutoLogin()).toBe(false);

    resetAutoLoginBlock();
    expect(shouldAutoLogin()).toBe(true);
  });
});