# 店小宝 MCP Server

把「店小宝 ERP Copilot」能力封装为 MCP（Model Context Protocol）服务，供扣子 Coze / 豆包插件市场等客户端远程调用。
支持：**一键复制并发布商品**（阿里巴巴国际站、速卖通、Lazada、Shopee、TikTok、Shopify、Temu、Miravia、敦煌网、亚马逊等授权店铺）、
**发品结果查询**、**关联店铺查询**、**图片上传 / 抠图 / 翻译 / 尺寸调整 / 结果查询**。

底层复用店小宝技能附带的 `scripts/dxb.py`（纯 Python 标准库），所有业务校验、接口请求、脱敏回执逻辑与原技能完全一致。

## 目录结构

```
D:\mcp\
├── server.py              # MCP Server 主程序（FastMCP）
├── requirements.txt       # 依赖（mcp、uvicorn）
├── config.json            # 用户密文、默认店铺、agent 名称
├── .env.example           # 环境变量示例（复制为 .env 后使用）
├── README.md
├── Dockerfile             # 容器化部署（可选）
├── nginx.conf.example     # Nginx 反向代理 + HTTPS 示例（可选）
└── scripts\
    ├── dxb.py             # 店小宝 CLI（来自技能，勿改）
    └── dxb_images.py      # 图片命令实现（来自技能，勿改）
```

## 快速开始（Windows）

```powershell
cd D:\mcp
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

### 1. 配置用户密文

编辑 `config.json`：

```json
{
  "agent_name": "dianxiaobao-mcp",
  "user_code": "你的用户密文",
  "store_name": "默认店铺名称（发品用，图片任务不需要）"
}
```

密文获取方式：打开 https://erp.dianxiaobao.net/ 登录店小宝账号 → 右上角账号菜单 → 账号管理 → 我的账号 → 用户密文。
也可用环境变量 `DXB_USER_CODE` 配置（优先级：工具参数 userCode > DXB_USER_CODE > config.json）。

### 2. 本地调试（stdio）

```powershell
MCP_TRANSPORT=stdio python server.py
```

然后可用 MCP Inspector 或支持 stdio 的 MCP 客户端连接（命令：`python D:\mcp\server.py`，环境变量 `MCP_TRANSPORT=stdio`）。

### 3. 本地启动 SSE（对接扣子前先自测）

```powershell
python server.py
# 默认监听 0.0.0.0:8000，SSE 端点：http://127.0.0.1:8000/sse
```

浏览器打开 `http://127.0.0.1:8000/sse` 能看到连接建立即为正常。

## 部署上线（供扣子/豆包公网接入）

远程 MCP 要求 **HTTPS + 备案域名**，不能使用 IP 或 http。推荐两种方式：

### 方式 A：Linux 服务器 + Nginx + systemd

1. 把本目录上传到服务器（如 `/opt/mcp`），安装依赖：

```bash
cd /opt/mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

2. 用 systemd 常驻运行（`/etc/systemd/system/dxb-mcp.service`）：

```ini
[Unit]
Description=Dianxiaobao MCP Server (SSE)
After=network.target

[Service]
WorkingDirectory=/opt/mcp
Environment=MCP_TRANSPORT=sse
Environment=MCP_PORT=8000
ExecStart=/opt/mcp/.venv/bin/python /opt/mcp/server.py
Restart=always

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload && systemctl enable --now dxb-mcp
```

3. Nginx 反向代理 + SSL（参考 `nginx.conf.example`，绑定备案域名如 `mcp.example.com`），
   把 `https://mcp.example.com/sse` 转发到 `127.0.0.1:8000`。

### 方式 B：Docker（可选）

```bash
docker build -t dxb-mcp .
docker run -d --name dxb-mcp -p 8000:8000 -e MCP_TRANSPORT=sse -v /opt/mcp/config.json:/app/config.json:ro dxb-mcp
```

## 接入扣子 Coze / 豆包插件市场

1. 扣子 → 资源库 → +资源 → 插件 → 创建 MCP 插件
2. 插件 URL 填：`https://你的域名/sse`（方式 A 部署的地址）
3. Header：按需添加鉴权（如 `Authorization: Bearer xxx`），无则保留默认
4. 授权方式：按业务选择；保存后进入调试面板，测试下列工具是否正常返回
5. 调试全部通过 → 发布 → 上架渠道选择「扣子插件商店（同步豆包工作插件市场）」→ 填写介绍、隐私声明 → 提交审核
6. 审核通过后，插件卡片即出现在豆包「插件·技能·伙伴」页面

## MCP 工具清单

| 工具 | 说明 | 关键参数 |
| --- | --- | --- |
| `check_user` | 检查密文是否已配置 | 无 |
| `query_stores` | 查询关联授权店铺（按平台分组） | user_code 可选 |
| `publish_product` | 一键发品（ai_release_products） | store_name、source_urls[] |
| `query_publish_status` | 查询发品任务结果 | store_name、plan_id |
| `upload_local_image` | 上传本地图片取链接（≤10MB） | file_path |
| `process_image` | 抠图 / 翻译 / 尺寸调整 | images[]、action 及对应参数 |
| `query_image_status` | 查询图片处理结果 | record_id |

## 注意事项

- 所有命令的退出码语义：0 正常 / 2 入参问题 / 3 接口拒绝 / 4 结果不确定 / 5 缺少密文；工具返回值会附带说明。
- `QUEUED / RUNNING` 仅表示已受理，**不代表发布或处理完成**，务必用返回的 planId / recordId 查询最终结果。
- 提交成功≠处理成功：发品以 `aiReleaseStatus` 的成功/失败数量为准，图片以 `state=2/FINISHED` + successCount/failCount 为准。
- 结果不确定（超时/网络错误）时**不要自动重试**，用已知任务ID查询或核对后台记录。
- `image-status` 只支持 recordId（图片任务ID），不要用发品 planId。
- 密文、token 不会回显；回执默认保存在系统临时目录 `dxb-ai-release` 下，可用于核对。
