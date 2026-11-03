"""ImageOpen requests and local upload encoding. No network or file writes."""
import hashlib
import json
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit


COMMANDS = ("image-submit", "image-status", "image-upload")
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
BASE = "https://api.dianxiaobao.net/api-user"
ACTIONS = ("agent_use_cut", "agent_use_translate", "agent_use_resize")
FIELDS = {
    "file_path": "filePath",
    "action": "action", "image_url": "images", "record_id": "recordId",
    "task_key": "taskKey", "src_lang": "srcLang", "tgt_lang": "tgtLang",
    "commodity_filter_on": "commodityFilterOn",
    "translate_brand": "translatingBrandInTheProduct", "scale_type": "scaleType",
    "side_type": "sideType", "change_length": "changeLength", "fit": "fit",
}
TRANSLATE_FIELDS = {"srcLang", "tgtLang", "commodityFilterOn", "translatingBrandInTheProduct", "num"}
RESIZE_FIELDS = {"scaleType", "sideType", "changeLength", "fit"}


def add_arguments(parser):
    parser.add_argument("--file", dest="file_path", help="image-upload 的单张本地图片路径，不接受图片 URL")
    parser.add_argument("--image-base-url", help="图片服务基础地址，优先于 DXB_IMAGE_API_BASE_URL 和 config.json 的 image_base_url")
    parser.add_argument("--action", choices=ACTIONS)
    parser.add_argument("--image-url", action="append", help="图片 URL，可重复指定")
    parser.add_argument("--record-id", help="图片任务 recordId，不是发品 planId")
    parser.add_argument("--task-key")
    parser.add_argument("--src-lang")
    parser.add_argument("--tgt-lang")
    parser.add_argument("--commodity-filter-on", choices=("0", "1"))
    parser.add_argument("--translate-brand", choices=("0", "1"))
    parser.add_argument("--scale-type", help="keepOld 或正整数比例，例如 3:4")
    parser.add_argument("--side-type", choices=("width", "height"))
    parser.add_argument("--change-length", help="目标边长，正整数像素")
    parser.add_argument("--fit", help="默认 contain；其他模式按已确认的图片服务契约使用")


def text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(field + " 必须为非空字符串")
    return value.strip()


def positive_integer(value, field, maximum=2147483647):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError(field + " 必须为正整数")
    # Check digit length before int conversion; reject accidental huge inputs.
    normalized = str(value).lstrip("0") or "0"
    if len(normalized) > len(str(maximum)):
        raise ValueError(field + " 超出接口整数范围")
    number = int(normalized)
    if not 0 < number <= maximum:
        raise ValueError(field + " 必须大于 0 且不超过 " + str(maximum))
    return number


def flag(value, field):
    if type(value) not in (str, int) or str(value) not in ("0", "1"):
        raise ValueError(field + " 仅支持 0 或 1")
    return str(value)


def validate_base(value):
    value = text(value, "图片接口基础地址")
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or re.search(r"\s|\\", value)):
        raise ValueError("图片接口基础地址必须为 HTTPS，不含账号密码、查询参数或片段")
    parsed.port  # Reject malformed ports before sending credentials.
    if "/imageOpen" in parsed.path:
        raise ValueError("图片接口基础地址不应包含 /imageOpen，请传入服务前缀")
    return value.rstrip("/")


def build_request(args, data, user_code):
    if args.store_name is not None or args.source_url is not None or args.plan_id is not None:
        raise ValueError("图片命令不使用 --store-name、--source-url 或 --plan-id")
    values = dict(data)
    for option, field in FIELDS.items():
        value = getattr(args, option)
        if value is not None:
            values[field] = value
    body = {"userCode": user_code}
    if args.command == "image-upload":
        if set(values) - {"userCode", "filePath"}:
            raise ValueError("图片上传只接受 userCode 和 filePath；图片链接直接使用 image-submit")
        body["filePath"] = text(values.get("filePath"), "filePath")
        return body, "/imageOpen/upload"
    if args.command == "image-status":
        unknown = set(values) - {"userCode", "recordId"}
        if unknown:
            raise ValueError("图片查询只接受 userCode 和 recordId，不支持 taskKey、planId 或店铺参数")
        body["recordId"] = str(positive_integer(values.get("recordId"), "recordId", 9223372036854775807))
        return body, "/imageOpen/status"

    action = values.get("action")
    if action not in ACTIONS:
        raise ValueError("图片 action 必须为 " + "、".join(ACTIONS))
    allowed = {"userCode", "action", "images", "taskKey"}
    if action == "agent_use_translate":
        allowed |= TRANSLATE_FIELDS
    elif action == "agent_use_resize":
        allowed |= RESIZE_FIELDS
    if set(values) - allowed:
        raise ValueError("参数包含当前图片操作不支持的字段；请检查操作类型，不要混入店铺、身份或其他操作参数")
    images = values.get("images")
    if isinstance(images, str):
        try:
            images = json.loads(images)
        except ValueError:
            raise ValueError("images 必须为图片 URL 数组或该数组的 JSON 字符串") from None
    if not isinstance(images, list) or not images:
        raise ValueError("images 必须为非空图片 URL 数组")
    for url in images:
        if not isinstance(url, str) or not url or re.search(r"\s|[\[\]\\]", url):
            raise ValueError("图片链接必须为纯 HTTP(S) URL，不含 Markdown、空白或粘贴转义")
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("图片链接必须为完整 HTTP(S) URL，不能包含账号密码")
        parsed.port
    body.update(action=action, images=images)
    if "taskKey" in values:
        body["taskKey"] = text(values["taskKey"], "taskKey")
    if action == "agent_use_translate":
        body["srcLang"] = text(values.get("srcLang", "zh"), "srcLang")
        body["tgtLang"] = text(values.get("tgtLang", "en"), "tgtLang")
        body["commodityFilterOn"] = flag(values.get("commodityFilterOn", "0"), "commodityFilterOn")
        body["translatingBrandInTheProduct"] = flag(values.get("translatingBrandInTheProduct", "0"), "translatingBrandInTheProduct")
        if type(values.get("num", 1)) not in (str, int) or str(values.get("num", 1)) != "1":
            raise ValueError("图片翻译每张仅允许 num=1；它不是批次图片数量")
        body["num"] = 1
    elif action == "agent_use_resize":
        scale = text(values.get("scaleType"), "scaleType")
        side = text(values.get("sideType"), "sideType").lower()
        if side not in ("width", "height"):
            raise ValueError("sideType 仅支持 width 或 height")
        length = positive_integer(values.get("changeLength"), "changeLength")
        if scale.lower() == "keepold":
            scale = "keepOld"
        else:
            match = re.fullmatch(r"([0-9]+):([0-9]+)", scale)
            if not match:
                raise ValueError("scaleType 必须为 keepOld 或正整数比例，例如 3:4")
            width = positive_integer(match[1], "比例宽度")
            height = positive_integer(match[2], "比例高度")
            factor, divisor = (height, width) if side == "width" else (width, height)
            if length * factor > 2147483647 or length * factor // divisor < 1:
                raise ValueError("目标尺寸会出现整数溢出或 0 像素，请调整比例或边长")
            scale = str(width) + ":" + str(height)
        body.update(scaleType=scale, sideType=side, changeLength=length, fit=text(values.get("fit", "contain"), "fit"))
    return body, "/imageOpen/submit"


def prepare_upload(body):
    file_path = body["filePath"]
    if re.match(r"(?i)^https?://", file_path):
        raise ValueError("图片链接直接使用 image-submit，不调用上传接口")
    path = Path(file_path).expanduser()
    if not path.is_file():
        raise ValueError("本地图片不存在或不是普通文件")
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        raise ValueError("图片大小不能超过10MB")
    with path.open("rb") as source:
        content = source.read(MAX_UPLOAD_BYTES + 1)
    if not content:
        raise ValueError("图片文件不能为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError("图片大小不能超过10MB")
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        suffix, mime = "png", "image/png"
    elif content.startswith(b"\xff\xd8\xff"):
        suffix, mime = "jpg", "image/jpeg"
    elif content.startswith((b"GIF87a", b"GIF89a")):
        suffix, mime = "gif", "image/gif"
    elif content.startswith(b"BM"):
        suffix, mime = "bmp", "image/bmp"
    elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        suffix, mime = "webp", "image/webp"
    else:
        raise ValueError("未识别到支持的图片文件头：PNG、JPEG、GIF、BMP、WebP；最终格式有效性由服务器校验")
    boundary = "dxb" + uuid.uuid4().hex
    # A generated ASCII filename avoids leaking paths or injecting multipart headers.
    prefix = ("--" + boundary + '\r\nContent-Disposition: form-data; name="userCode"\r\n\r\n'
              + body["userCode"] + "\r\n--" + boundary
              + '\r\nContent-Disposition: form-data; name="file"; filename="upload.' + suffix
              + '"\r\nContent-Type: ' + mime + '\r\n\r\n').encode("utf-8")
    payload = prefix + content + ("\r\n--" + boundary + "--\r\n").encode("ascii")
    metadata = {"filePath": str(path.resolve()), "size": len(content), "contentType": mime,
                "sha256": hashlib.sha256(content).hexdigest()}
    return payload, "multipart/form-data; boundary=" + boundary, metadata


def uploaded_url(result):
    data = result.get("data")
    url = data.get("imageUrl") if isinstance(data, dict) else None
    if not isinstance(url, str) or not url or re.search(r"\s|[\[\]\\]", url):
        raise ValueError("上传响应缺少有效 data.imageUrl，不能继续图片处理")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("上传响应 imageUrl 不是有效 HTTP(S) 图片链接")
    parsed.port
    return url
