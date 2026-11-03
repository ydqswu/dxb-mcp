"""CLI for Dianxiaobao publishing, stores and image tasks. Standard library only."""
import argparse
import json
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from urllib import request
from urllib.parse import urlsplit
import dxb_images

BASE = "https://goods.dianxiaobao.net/api-goods"
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"
NOTICE_BAR = "=" * 60
MISSING_USER_CODE_NOTICE = "\n".join([
    "",
    NOTICE_BAR,
    ">>> 缺少密文（userCode）：需要你先配置，本次调用已停止 <<<",
    NOTICE_BAR,
    "未配置 userCode，请获取并配置店小宝用户密文：",
    "1. 打开 https://erp.dianxiaobao.net/，注册或登录自己的店小宝账号。",
    "2. 打开页面右上角的账号菜单，点击“账号管理”。",
    "3. 进入“我的账号”页面，找到“用户密文”一栏，复制自己的用户密文。",
    "4. 打开本技能目录下的 config.json，将密文填写到 user_code；发品时可将默认店铺名称填写到 store_name，图片任务不需要店铺，保存文件。",
    "5. 重新运行 check-user 检查配置，然后再提交发品或查询任务。",
    "也可通过 DXB_USER_CODE 环境变量或请求参数 userCode 配置密文。",
    "用户密文不是登录密码，也不是推荐邀请码；请勿使用示例截图中的密文。",
    NOTICE_BAR,
    ">>> 配置完成后重新发起请求；在此之前不要执行发品或图片命令 <<<",
    NOTICE_BAR,
    "",
])


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def optional_text(value, field):
    if value is not None and not isinstance(value, str):
        raise ValueError(field + " 必须为字符串，类型错误不会回退到默认配置")
    return value


def detect_agent_name(runtime_name=None):
    # The host can supply its identity; never infer it from product/user data.
    if nonempty(runtime_name):
        return runtime_name.strip()
    if nonempty(os.getenv("DXB_AGENT_NAME")):
        return os.environ["DXB_AGENT_NAME"].strip()
    if nonempty(os.getenv("CODEX_THREAD_ID")):
        return "Codex"
    raise ValueError("无法识别当前 agent 工具：请由宿主工具自动注入 --agent-name 或 DXB_AGENT_NAME 后重试，无需用户填写。")


def agent_name(runtime_name=None, save=False):
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")) if CONFIG_PATH.exists() else {}
    if not isinstance(config, dict):
        raise ValueError("config.json 必须为 JSON 对象")
    name = config.get("agent_name")
    if name is not None and not isinstance(name, str):
        raise ValueError("config.json 中 agent_name 必须为字符串")
    if nonempty(name):
        return name.strip()
    name = detect_agent_name(runtime_name)
    if save:
        config["agent_name"] = name
        CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return name


def scrub(value, secrets):
    if isinstance(value, dict):
        return {k: "[REDACTED]" if re.search(r"usercode|token|secret|password|authorization", k, re.I)
                else scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
    return value


def plain(value):
    return re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", str(value)) if value is not None else "未提供"


def format_stores(data):
    if not isinstance(data, dict):
        raise ValueError("店铺数据应为按平台分组的对象")
    total = 0
    lines = []
    for platform, stores in data.items():
        if not isinstance(stores, list) or any(not isinstance(store, dict) for store in stores):
            raise ValueError("平台店铺数据应为对象数组")
        lines.append("\n【" + plain(platform) + "】（" + str(len(stores)) + " 家）")
        for index, store in enumerate(stores, 1):
            lines.append(f"  {index}. 店铺：{plain(store.get('storeName'))} | 店铺 ID：{plain(store.get('id'))} | 平台标识：{plain(store.get('ptName'))}")
        total += len(stores)
    if total == 0:
        return "查询成功，当前账号暂无关联店铺。"
    return f"查询成功，共 {total} 家关联店铺。\n" + "\n".join(lines)


def main(argv=None, transport=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("command", choices=["check-user", "stores", "submit", "status", "configure-agent"] + list(dxb_images.COMMANDS))
    p.add_argument("--agent-name", help="宿主工具自动注入的身份；用户无需填写，仅在配置为空时使用")
    p.add_argument("--params")
    p.add_argument("--store-name")
    p.add_argument("--source-url", action="append")
    p.add_argument("--plan-id")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--receipt")
    p.add_argument("--base-url", help="发品和店铺接口基础地址；图片接口使用 --image-base-url")
    dxb_images.add_arguments(p)
    a = p.parse_args(argv)
    try:
        is_image = a.command in dxb_images.COMMANDS
        if is_image and a.base_url is not None:
            raise ValueError("图片命令请使用 --image-base-url，避免混用发品地址")
        if not is_image and (a.image_base_url is not None or any(getattr(a, key) is not None for key in dxb_images.FIELDS)):
            raise ValueError("图片参数仅用于 image-upload、image-submit 或 image-status")
        if a.command == "configure-agent":
            print("agentName：" + plain(agent_name(a.agent_name, save=not a.preview)))
            return 0
        # Bind before user/account checks, including the first check-user call.
        resolved_agent = agent_name(a.agent_name, save=not a.preview)
        data = json.loads(Path(a.params).read_text(encoding="utf-8-sig")) if a.params else {}
        if not isinstance(data, dict):
            raise ValueError("参数文件必须为 JSON 对象")
        code = optional_text(data.get("userCode"), "userCode")
        if not nonempty(code):
            code = os.getenv("DXB_USER_CODE")
        if not nonempty(code) and CONFIG_PATH.exists():
            config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if not isinstance(config, dict):
                raise ValueError("config.json 必须为 JSON 对象")
            code = config.get("user_code")
            if code is not None and not isinstance(code, str):
                raise ValueError("config.json 中 user_code 必须为字符串")
        if not nonempty(code):
            print(MISSING_USER_CODE_NOTICE)
            return 5
        if a.command == "check-user":
            print("userCode 已配置（未回显；账号有效性由接口验证）。")
            return 0
        if is_image:
            body, endpoint = dxb_images.build_request(a, data, code)
        elif a.command == "stores":
            body = {"userCode": code}
            endpoint = "getStoreListByUserCode"
        else:
            store = a.store_name if nonempty(a.store_name) else optional_text(data.get("storeName"), "storeName")
            if not nonempty(store) and CONFIG_PATH.exists():
                config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                if not isinstance(config, dict):
                    raise ValueError("config.json 必须为 JSON 对象")
                store = config.get("store_name")
                if store is not None and not isinstance(store, str):
                    raise ValueError("config.json 中 store_name 必须为字符串")
            if not nonempty(store):
                raise ValueError("缺少店铺名称，请传入 storeName、--store-name 或配置 config.json 的 store_name")
            body = {"userCode": code, "storeName": store}
        if a.command == "submit":
            if data.get("action") not in (None, "ai_release_products"):
                raise ValueError("本技能只支持 action=ai_release_products")
            urls = a.source_url if a.source_url is not None else data.get("sourceUrlList")
            if not isinstance(urls, list) or not urls:
                raise ValueError("sourceUrlList 必须为非空链接数组")
            for url in urls:
                if not nonempty(url) or re.search(r"\s|[\[\]\\]", url):
                    raise ValueError("商品链接必须为纯 URL，不含 Markdown、空白或粘贴转义")
                parsed = urlsplit(url)
                if parsed.scheme not in ("http", "https") or not parsed.hostname:
                    raise ValueError("商品链接必须为完整 HTTP(S) URL")
            body.update(action="ai_release_products", sourceUrlList=urls)
            body["agentName"] = resolved_agent
            endpoint = "submit"
        elif a.command == "status":
            plan = a.plan_id if a.plan_id is not None else data.get("planId")
            if isinstance(plan, bool) or not isinstance(plan, (str, int)) or not str(plan).strip():
                raise ValueError("缺少有效任务 ID planId")
            body["planId"] = str(plan)
            endpoint = "aiReleaseStatus"
        if is_image:
            image_base = a.image_base_url or os.getenv("DXB_IMAGE_API_BASE_URL")
            if not image_base and CONFIG_PATH.exists():
                config = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
                image_base = optional_text(config.get("image_base_url"), "image_base_url")
            url = dxb_images.validate_base(image_base or dxb_images.BASE) + endpoint
        else:
            api_base = a.base_url or os.getenv("DXB_API_BASE_URL") or BASE
            base = urlsplit(api_base)
            if base.scheme not in ("http", "https") or not base.hostname or base.username or base.password or base.query or base.fragment:
                raise ValueError("基础地址格式无效")
            url = api_base.rstrip("/") + "/productBatchOpenAgent/" + endpoint
        token = os.getenv("DXB_API_TOKEN", "")
        secrets = (code, token)
        content_type = "application/json; charset=utf-8"
        if a.command == "image-upload":
            payload, content_type, file_info = dxb_images.prepare_upload(body)
            request_info = {"userCode": code, "file": file_info}
        else:
            payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
            request_info = body
        snapshot = {"url": url, "request": scrub(request_info, secrets), "state": "PREVIEW" if a.preview else "REQUEST_PENDING"}
        if a.preview:
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            return 0
        receipt = Path(a.receipt) if a.receipt else Path(tempfile.gettempdir()) / "dxb-ai-release" / (uuid.uuid4().hex + ".json")
        if not a.receipt:
            receipt.parent.mkdir(parents=True, exist_ok=True)
        with receipt.open("x", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)
        headers = {"Content-Type": content_type, "Accept": "application/json"}
        if token:
            headers["token"] = token
        req = request.Request(url, payload, headers, method="POST")
    except (OSError, ValueError) as exc:
        print(str(exc) if type(exc) is ValueError else "参数文件、config.json、JSON 格式或回执路径无效；回执文件不可重复使用。")
        return 2
    result_code = 4
    try:
        opener = transport or request.build_opener(NoRedirect()).open
        with opener(req, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8-sig"))
        snapshot["response"] = scrub(result, secrets)
        if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
            raise ValueError("Unexpected response")
        detail = result.get("data")
        rejected = not result["success"] or (isinstance(detail, dict) and
                    (detail.get("accepted") is False or detail.get("isSuccess") in (0, "0")))
        if a.command == "stores" and not rejected:
            store_text = format_stores(scrub(detail, secrets))
        if a.command == "image-upload" and not rejected:
            dxb_images.uploaded_url(result)
            snapshot["message"] = "图片已上传，尚未提交处理；使用 data.imageUrl 调用 image-submit。"
        snapshot.update(state="REJECTED" if rejected else "RESPONSE_RECEIVED", response=scrub(result, secrets))
        if rejected and a.command == "image-submit":
            snapshot["message"] = "图片提交被拒绝，但后台可能已创建批次或发送部分图片；请核对记录，不要自动重提。"
        result_code = 3 if rejected else 0
    except Exception as exc:
        snapshot.update(state="OUTCOME_UNKNOWN", errorType=type(exc).__name__,
                        message="结果不确定，不要自动重新提交；图片任务使用已知 recordId，发品任务使用已知 planId 查询或核对后台记录。")
        if a.command == "image-upload":
            snapshot["message"] = "上传结果不确定或缺少有效图片链接；停止后续处理，不自动重新上传，保留回执核对结果。"
    try:
        receipt.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        snapshot["receiptWriteFailed"] = True
        result_code = 4
    snapshot["receipt"] = str(receipt)
    if a.command == "stores":
        if snapshot["state"] == "RESPONSE_RECEIVED":
            print(store_text)
        elif snapshot["state"] == "REJECTED":
            print("店铺查询失败：" + plain(snapshot["response"].get("errorMessage")))
        else:
            print("店铺查询结果不确定，请稍后查询；没有触发发品。")
        if snapshot.get("receiptWriteFailed"):
            print("回执保存失败。")
        else:
            print("回执：" + str(receipt))
    else:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    return result_code


if __name__ == "__main__":
    sys.exit(main())
