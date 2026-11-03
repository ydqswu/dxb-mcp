"""店小宝 MCP Server
====================

将「店小宝 ERP Copilot」能力包装为 MCP（Model Context Protocol）工具：
商品发品、发品结果查询、关联店铺查询、图片上传、图片抠图/翻译/尺寸调整、图片结果查询。

底层复用店小宝技能附带的 scripts/dxb.py 与 scripts/dxb_images.py（纯标准库），
所有业务校验、接口请求、脱敏回执逻辑均沿用原脚本，MCP 层只负责参数透传与结果返回。

传输方式（环境变量 MCP_TRANSPORT 控制，默认 sse）：
  - sse              : SSE 传输，供扣子 Coze / 豆包插件市场远程接入（生产环境须经 Nginx 反代为 HTTPS 域名）
  - streamable-http  : Streamable HTTP 传输，同样可接入 Coze
  - stdio            : 本地调试（Claude Desktop / MCP Inspector 等）

运行：
  pip install -r requirements.txt
  python server.py                          # SSE，监听 0.0.0.0:8000，端点 /sse
  MCP_TRANSPORT=stdio python server.py      # 本地 stdio 调试
"""
from __future__ import annotations
from mcp.server.transport_security import TransportSecuritySettings

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DXB_SCRIPT = PROJECT_ROOT / "scripts" / "dxb.py"

SERVER_NAME = "dianxiaobao"
SERVER_DESC = (
    "店小宝跨境ERP Copilot：一键复制并发布商品到阿里巴巴国际站、速卖通、Lazada、Shopee、TikTok、"
    "Shopify、Temu、Miravia、敦煌网、亚马逊等授权店铺，并支持商品图片抠图、翻译、尺寸调整与结果查询。"
    "使用前需配置用户密文 userCode。"
)

# host/port 在 FastMCP 构造时生效（SSE/streamable-http 监听地址）
MCP_HOST = os.getenv("MCP_HOST", "0.0.0.0")
MCP_PORT = int(os.getenv("MCP_PORT", "8000"))

# mcp = FastMCP(SERVER_NAME, instructions=SERVER_DESC, host=MCP_HOST, port=MCP_PORT)
# 允许通过 Nginx 反代暴露的公网域名访问（DNS Rebinding 保护白名单）
ALLOWED_HOSTS = [
    "localhost",
    "localhost:*",
    "127.0.0.1",
    "127.0.0.1:*",
    "dxbmcp.dianxiaobao.net",
    "dxbmcp.dianxiaobao.net:*",
]

ALLOWED_ORIGINS = [
    "http://localhost",
    "http://localhost:*",
    "http://127.0.0.1",
    "http://127.0.0.1:*",
    "https://dxbmcp.dianxiaobao.net",
    "https://dxbmcp.dianxiaobao.net:*",
]

transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=ALLOWED_ORIGINS,
)

mcp = FastMCP(
    SERVER_NAME,
    instructions=SERVER_DESC,
    host=MCP_HOST,
    port=MCP_PORT,
    transport_security=transport_security,
)


# --------------------------------------------------------------------------- #
# 底层调用：把 dxb.py 作为子进程执行，沿用其全部校验与业务逻辑
# --------------------------------------------------------------------------- #

def read_config() -> dict:
    """读取工程根目录 config.json，异常时返回空字典。"""
    if not CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def resolve_agent_name() -> str:
    """MCP Server 作为宿主工具，向 dxb.py 注入 agentName（不询问用户）。

    优先级：环境变量 DXB_AGENT_NAME -> config.json agent_name -> 默认 dianxiaobao-mcp
    """
    name = (os.getenv("DXB_AGENT_NAME") or "").strip()
    if name:
        return name
    cfg_name = read_config().get("agent_name")
    if isinstance(cfg_name, str) and cfg_name.strip():
        return cfg_name.strip()
    return "dianxiaobao-mcp"


def dxb_env() -> dict:
    env = os.environ.copy()
    env["DXB_AGENT_NAME"] = resolve_agent_name()
    return env


def run_dxb(command: str, params: Optional[dict] = None) -> dict:
    """执行 dxb.py 单条命令，返回 {exit_code, stdout, stderr, payload}。

    exit_code 语义（与 dxb.py 一致）：
      0 检查通过 / 接口返回响应；2 入参问题；3 明确拒绝；4 结果不确定；5 缺少用户密文
    """
    args = [sys.executable, "-X", "utf8", str(DXB_SCRIPT), command]
    tmp_params = None
    if params:
        fd, tmp_params = tempfile.mkstemp(suffix=".json", prefix="dxb-mcp-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(params, f, ensure_ascii=False)
            args += ["--params", tmp_params]
        except Exception:
            if tmp_params:
                try:
                    os.unlink(tmp_params)
                except OSError:
                    pass
            raise
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            env=dxb_env(),
            timeout=180,
        )
    finally:
        if tmp_params:
            try:
                os.unlink(tmp_params)
            except OSError:
                pass
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    payload = None
    if out:
        try:
            payload = json.loads(out)
        except ValueError:
            payload = out
    return {"exit_code": proc.returncode, "stdout": out, "stderr": err, "payload": payload}


def build_result(r: dict) -> str:
    """把子进程结果整理成 MCP 工具返回值（文本）。"""
    parts = []
    if r["stderr"]:
        parts.append("stderr: " + r["stderr"])
    if isinstance(r["payload"], str):
        parts.append(r["payload"])
    elif r["payload"] is not None:
        parts.append(json.dumps(r["payload"], ensure_ascii=False, indent=2))
    else:
        parts.append(r["stdout"] or "(无输出)")
    text = "\n".join(parts)
    code = r["exit_code"]
    if code == 5:
        return text + "\n\n【缺少用户密文】请先按上方提示获取并配置 userCode 后重试；不要臆造密文。"
    if code == 2:
        return text + "\n\n【入参问题】请按提示修正参数后重试。"
    if code == 3:
        return text + "\n\n【接口拒绝】业务方拒绝了本次请求，请按响应内容处理，不要自动重试。"
    if code == 4:
        return text + "\n\n【结果不确定】未得到确定性结果，请用已知任务ID（planId/recordId）查询或核对后台记录。"
    return text


# --------------------------------------------------------------------------- #
# MCP 工具
# --------------------------------------------------------------------------- #

@mcp.tool()
def check_user() -> str:
    """检查店小宝用户密文(userCode)是否已配置（不验证账号有效性）。未配置时返回获取密文的操作步骤。"""
    return build_result(run_dxb("check-user"))


@mcp.tool()
def query_stores(user_code: Optional[str] = None) -> str:
    """查询店小宝账号关联的所有授权店铺（按平台分组，含店铺名称与店铺ID）。发品前用它确认目标店铺的准确名称。

    Args:
        user_code: 店小宝用户密文；不传则使用环境变量 DXB_USER_CODE 或 config.json 中的 user_code。
    """
    params = {}
    if user_code:
        params["userCode"] = user_code
    return build_result(run_dxb("stores", params=params))


@mcp.tool()
def publish_product(store_name: str, source_urls: list[str], user_code: Optional[str] = None) -> str:
    """把 1 个或多个商品链接（1688/Alibaba/Amazon/AliExpress 等货源页完整 URL）一键发布到指定授权店铺。
    固定使用 ai_release_products 动作。返回任务ID(planId)，随后用 query_publish_status 查询处理结果。

    Args:
        store_name: 目标店铺名称，必须是 query_stores 返回的授权店铺准确名称。
        source_urls: 商品链接数组，每条必须是完整 HTTP(S) URL（如 https://detail.1688.com/offer/xxx.html）。
        user_code: 店小宝用户密文；不传则使用环境变量 DXB_USER_CODE 或 config.json 中的 user_code。
    """
    params = {"storeName": store_name, "sourceUrlList": source_urls}
    if user_code:
        params["userCode"] = user_code
    return build_result(run_dxb("submit", params=params))


@mcp.tool()
def query_publish_status(store_name: str, plan_id: str, user_code: Optional[str] = None) -> str:
    """查询发品任务结果。plan_id 必须是 publish_product 返回的真实任务ID；store_name 必须是该任务实际所属店铺。
    注意：QUEUED/RUNNING 仅表示已受理，不代表发布完成，需以成功/失败数量为准。

    Args:
        store_name: 该发品任务实际所属的店铺名称。
        plan_id: 发品时返回的任务ID。
        user_code: 店小宝用户密文；不传则使用环境变量 DXB_USER_CODE 或 config.json 中的 user_code。
    """
    params = {"storeName": store_name, "planId": plan_id}
    if user_code:
        params["userCode"] = user_code
    return build_result(run_dxb("status", params=params))


@mcp.tool()
def upload_local_image(file_path: str, user_code: Optional[str] = None) -> str:
    """上传 MCP 服务器本地的单张商品图片到店小宝（支持 PNG/JPEG/GIF/BMP/WebP，≤10MB）。
    成功返回 data.imageUrl，供 process_image 处理。仅接受本地文件路径，不接受 URL；图片URL请直接走 process_image。

    Args:
        file_path: 服务器上本地图片的绝对路径（如 D:/images/product.png）。
        user_code: 店小宝用户密文；不传则使用环境变量 DXB_USER_CODE 或 config.json 中的 user_code。
    """
    params = {"filePath": file_path}
    if user_code:
        params["userCode"] = user_code
    return build_result(run_dxb("image-upload", params=params))


@mcp.tool()
def process_image(
    images: list[str],
    action: str,
    src_lang: Optional[str] = None,
    tgt_lang: Optional[str] = None,
    commodity_filter_on: Optional[str] = None,
    translate_brand: Optional[str] = None,
    scale_type: Optional[str] = None,
    side_type: Optional[str] = None,
    change_length: Optional[int] = None,
    fit: Optional[str] = None,
    task_key: Optional[str] = None,
    user_code: Optional[str] = None,
) -> str:
    """对图片URL数组执行图片处理，返回 recordId 供 query_image_status 查询结果。
    action 取值：
      - agent_use_cut        抠图（无额外参数）
      - agent_use_translate  图片翻译（默认中文转英文；可选 src_lang/tgt_lang/commodity_filter_on/translate_brand）
      - agent_use_resize     尺寸调整（必填 scale_type、side_type、change_length；fit 默认 contain）
    图片必须是完整 HTTP(S) URL；本地图片请先用 upload_local_image 上传取链接。
    提交成功不代表处理完成，需用返回的 recordId 查询。

    Args:
        images: 图片URL数组。
        action: 操作类型，见上。
        src_lang: 翻译源语言代码（默认 zh）。
        tgt_lang: 翻译目标语言代码（默认 en）。
        commodity_filter_on: "0"或"1"，1 使用 PRO 分支与 T27 权益（默认 "0"）。
        translate_brand: "0"或"1"，是否翻译图中品牌（默认 "0"；PRO 分支固定翻译品牌）。
        scale_type: 调整尺寸比例，keepOld 或正整数比例如 "3:4"。
        side_type: 以哪边为基准，仅 width 或 height。
        change_length: 目标边长（正整数像素）。
        fit: 适配方式，默认 contain。
        task_key: 可选任务标识（非幂等键）。
        user_code: 店小宝用户密文；不传则使用环境变量 DXB_USER_CODE 或 config.json 中的 user_code。
    """
    params = {"action": action, "images": images}
    if src_lang is not None:
        params["srcLang"] = src_lang
    if tgt_lang is not None:
        params["tgtLang"] = tgt_lang
    if commodity_filter_on is not None:
        params["commodityFilterOn"] = commodity_filter_on
    if translate_brand is not None:
        params["translatingBrandInTheProduct"] = translate_brand
    if scale_type is not None:
        params["scaleType"] = scale_type
    if side_type is not None:
        params["sideType"] = side_type
    if change_length is not None:
        params["changeLength"] = change_length
    if fit is not None:
        params["fit"] = fit
    if task_key is not None:
        params["taskKey"] = task_key
    if user_code:
        params["userCode"] = user_code
    return build_result(run_dxb("image-submit", params=params))


@mcp.tool()
def query_image_status(record_id: str, user_code: Optional[str] = None) -> str:
    """查询图片处理任务结果。record_id 必须是 process_image 返回的真实任务ID（不是发品planId）。
    判断：state=1/RUNNING 处理中；state=2/FINISHED 处理结束（可能部分失败，看 successCount/failCount 与 failReason）。

    Args:
        record_id: 图片处理任务ID。
        user_code: 店小宝用户密文；不传则使用环境变量 DXB_USER_CODE 或 config.json 中的 user_code。
    """
    params = {"recordId": record_id}
    if user_code:
        params["userCode"] = user_code
    return build_result(run_dxb("image-status", params=params))


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #

def main() -> None:
    transport = (os.getenv("MCP_TRANSPORT") or "sse").strip().lower()
    if transport == "stdio":
        mcp.run(transport="stdio")
    elif transport == "streamable-http":
        # 端点 /mcp
        mcp.run(transport="streamable-http")
    else:
        # SSE 端点 /sse —— 扣子/豆包插件市场填写的插件URL
        mcp.run(transport="sse")


if __name__ == "__main__":
    main()
