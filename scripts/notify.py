"""签到结果推送（零依赖，只用标准库）。

支持渠道，配了哪个推哪个：

- Server酱      SERVERCHAN_SENDKEY   （微信服务号）
- PushPlus      PUSHPLUS_TOKEN       （微信）
- WxPusher      WXPUSHER_APPTOKEN + WXPUSHER_UID（微信）
- Qmsg酱        QMSG_KEY [+ QMSG_QQ] （QQ）
- 自定义 webhook NOTIFY_WEBHOOK       （钉钉 / 飞书 / 企业微信机器人）

推送策略：**只在需要你知道的时候推**。
这个仓库一天有三个 cron 时段，如果每次都推，Server酱 免费额度（5 条/天）
会被迅速吃光。所以默认只推「新领取成功」「活动未开启」「报错」，
「今日已签到」这种正常冗余不推。想看全部就设 NOTIFY_ALWAYS=1。

另外：推送失败永不改变签到本身的退出码——通知是旁路，不能因为它让任务变红。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_SENT = False

TITLES = {
    "CLAIMED": "WorkBuddy 签到成功",
    "ALREADY": "WorkBuddy 今日已签到",
    "INACTIVE": "WorkBuddy 签到未开启",
    "ERROR": "WorkBuddy 签到失败",
}
# 默认推送的结果；ALREADY 属于「一切正常」的冗余信息，默认不推
DEFAULT_PUSH_RESULTS = {"CLAIMED", "INACTIVE", "ERROR"}


def _post(url: str, data: dict[str, Any], as_form: bool = False, timeout: int = 15) -> tuple[int, str]:
    body = urllib.parse.urlencode(data).encode() if as_form else json.dumps(data, ensure_ascii=False).encode()
    ctype = "application/x-www-form-urlencoded" if as_form else "application/json"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("content-type", ctype)
    req.add_header("user-agent", "workbuddy-auto-checkin/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")[:200]


def _push_channels(title: str, text: str) -> list[str]:
    sent: list[str] = []

    key = os.environ.get("SERVERCHAN_SENDKEY", "").strip()
    if key:
        url = key if key.startswith("http") else f"https://sctapi.ftqq.com/{key}.send"
        try:
            status, body = _post(url, {"title": title, "desp": text}, as_form=True)
            if status == 200 and '"code":0' in body.replace(" ", ""):
                sent.append("Server酱")
            else:
                print(f"::warning::Server酱 推送异常：{body}", flush=True)
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响主流程
            print(f"::warning::Server酱 推送失败：{type(exc).__name__} {exc}", flush=True)

    token = os.environ.get("PUSHPLUS_TOKEN", "").strip()
    if token:
        try:
            status, body = _post(
                "https://www.pushplus.plus/send",
                {"token": token, "title": title, "content": text, "template": "txt"},
            )
            if status == 200 and '"code":200' in body.replace(" ", ""):
                sent.append("PushPlus")
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::PushPlus 推送失败：{type(exc).__name__} {exc}", flush=True)

    app_token = os.environ.get("WXPUSHER_APPTOKEN", "").strip()
    uid = os.environ.get("WXPUSHER_UID", "").strip()
    if app_token and uid:
        try:
            status, _ = _post(
                "https://wxpusher.zjiecode.com/api/send/message",
                {
                    "appToken": app_token,
                    "content": f"## {title}\n\n{text}",
                    "summary": title[:20],
                    "contentType": 3,
                    "uids": [uid],
                },
            )
            if status == 200:
                sent.append("WxPusher")
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::WxPusher 推送失败：{type(exc).__name__} {exc}", flush=True)

    qmsg = os.environ.get("QMSG_KEY", "").strip()
    if qmsg:
        url = qmsg if qmsg.startswith("http") else f"https://qmsg.zendee.cn/v3/send/{qmsg}"
        payload: dict[str, Any] = {"msg": f"{title}\n{text}"}
        target = os.environ.get("QMSG_QQ", "").strip()
        if target:
            payload["qq"] = target
        try:
            status, body = _post(url, payload, as_form=True)
            if status == 200 and ('"success":true' in body.replace(" ", "") or '"code":0' in body.replace(" ", "")):
                sent.append("Qmsg酱")
        except Exception as exc:  # noqa: BLE001
            print(f"::warning::Qmsg酱 推送失败：{type(exc).__name__} {exc}", flush=True)

    hook = os.environ.get("NOTIFY_WEBHOOK", "").strip()
    if hook:
        for payload in (
            {"msgtype": "text", "text": {"content": f"{title}\n{text}"}},   # 钉钉
            {"msg_type": "text", "content": {"text": f"{title}\n{text}"}},  # 飞书
            {"text": f"{title}\n{text}"},                                    # 企业微信 / 自定义
        ):
            try:
                status, _ = _post(hook, payload)
                if status < 400:
                    sent.append("Webhook")
                    break
            except Exception:  # noqa: BLE001
                continue

    return sent


def notify_payload(payload: dict[str, Any]) -> None:
    """根据签到 payload 决定要不要推、推什么。任何异常都吞掉。"""
    global _SENT
    if _SENT:
        return

    result = str(payload.get("result") or "").upper()
    report = str(payload.get("report") or "").strip()

    always = os.environ.get("NOTIFY_ALWAYS", "").strip() in {"1", "true", "yes"}
    if not always and result not in DEFAULT_PUSH_RESULTS:
        return

    title = TITLES.get(result, "WorkBuddy 签到")
    lines = [report] if report else []
    wallet = payload.get("wallet") if isinstance(payload.get("wallet"), dict) else None
    if wallet:
        total = wallet.get("total")
        if total is not None:
            lines.append(f"账户总额：{total}")

    # 领到了积分但活跃地图没点亮：签到脚本会以非 0 退出，通知里要说清楚
    growth = payload.get("growth") if isinstance(payload.get("growth"), dict) else {}
    if result == "CLAIMED" and growth.get("calendar_lit") is False:
        lines.append("⚠ 活跃地图今天还没点亮（这一格仍是空的），建议去 Actions 手动补跑一次")

    text = "\n".join(lines) or "（无详情）"
    if result == "ERROR":
        text += "\n\n请去 Actions 日志看具体报错，多半是凭据需要重新获取。"

    try:
        sent = _push_channels(title, text)
    except Exception as exc:  # noqa: BLE001
        print(f"::warning::通知推送整体失败：{type(exc).__name__} {exc}", flush=True)
        return

    _SENT = True
    if sent:
        print(f"通知已推送：{'、'.join(sent)}", flush=True)
    elif any(os.environ.get(k, "").strip() for k in (
            "SERVERCHAN_SENDKEY", "PUSHPLUS_TOKEN", "WXPUSHER_APPTOKEN", "QMSG_KEY", "NOTIFY_WEBHOOK")):
        print("通知没有推送成功，检查 key/额度", flush=True)


def main(argv: list[str] | None = None) -> int:
    """命令行入口：给 workflow 的 `if: failure()` 兜底步骤用。

    `python scripts/notify.py --fallback` 发一条"工作流失败"的消息。
    这条路径不依赖签到脚本本身，所以哪怕 Python 步骤崩了、依赖没装上，
    只要通知 Secret 配了，微信仍然收得到。
    """
    argv = list(argv if argv is not None else os.sys.argv[1:])
    if "--fallback" in argv:
        step = os.environ.get("FAILED_STEP", "").strip()
        report = "GitHub Actions 运行失败。"
        if step:
            report += f"失败步骤：{step}。"
        report += "常见原因是登录态失效、接口变动或网络波动，请打开运行日志查看具体报错。"
        notify_payload({"result": "ERROR", "report": report})
        return 0
    print("用法：python scripts/notify.py --fallback")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
