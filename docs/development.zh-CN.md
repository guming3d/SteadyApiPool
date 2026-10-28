[← SteadyApiPool](../README.zh-CN.md)

[English](development.md) | **简体中文**

# 本地运行与开发

## 项目结构

```
SteadyApiPool/
├── backend/    FastAPI 应用（pool_manager：共享代码、apim/ 和 litellm/）及测试；同时提供构建好的
│               前端
├── frontend/   React/Vite 界面（src/apim、src/litellm 和其余共享部分；构建到
│               backend/pool_manager/static）
├── infra/      Bicep：容器应用和 AKS 上的 LiteLLM 网关（infra/gateway），以及用 Easy Auth 和最小权限
│               角色把本应用托管在 App Service 上
├── deploy/     容器应用上的公开多租户应用（仅 APIM；独立的 azd 项目）
├── scripts/    本地启动脚本、doctor、演示服务器、性能测试、发布打包和网关模板构建
├── docs/       本 README 的截图
└── azure.yaml  azd 项目定义（App Service 托管）
```

## 本地运行

在本地，应用运行在你的电脑上，并以**你**的身份操作 Azure。它只需要 **Python 3.11+**。只有开发浏览器界面时才需要
Node.js，Azure CLI 是可选的。

**最快的方式：发布包。** 从 [Releases](https://github.com/guming3d/SteadyApiPool/releases) 下载
`SteadyApiPool-<version>.zip`。它内置了浏览器界面和网关模板，因此不需要 Node.js 和 git。解压后运行：

```powershell
scripts\start-local.cmd                 # Windows；不受 PowerShell 执行策略影响
```

```sh
scripts/start-local.sh                  # macOS、Linux、WSL
```

打开 http://127.0.0.1:8000。设置页会先让你登录，然后让你选择网关类型和网关，或新建、连接网关（选择保存在
`backend/.pool-manager.json` 中，其中从不包含主密钥；连接的 LiteLLM 网关的密钥由操作系统加密保存在
`backend/.pool-manager-gateways.bin` 中）。如果要固定一个网关，请在 `backend/.env` 中设置
`POOL_MANAGER_SUBSCRIPTION_ID` 和 `_RESOURCE_GROUP`，再加上 `_APIM_NAME` 或 `_GATEWAY_NAME`（或者
`_GATEWAY_NAME` 和 `_GATEWAY_URL`）；`backend/.env.example` 给出了每种写法。

**在克隆的仓库中**，安装了 Node.js 22+ 时，同样的脚本会在 `:5174` 上运行 Vite 开发服务器。没有 Node.js 时，
如果界面已经构建过，它们会从 `backend/pool_manager/static` 提供界面（`-Prebuilt` / `--prebuilt` 强制如此）。

```powershell
.\scripts\start-local.ps1               # -Prebuilt -BackendPort 8001 -FrontendPort 5175 -SkipInstall -SkipChecks
```

```sh
scripts/start-local.sh                  # --prebuilt --backend-port 8001 --frontend-port 5175 --skip-install --skip-checks
```

脚本会先运行 `scripts/doctor.py --brief`。它只使用标准库，因此在安装任何东西之前、甚至在较旧的 Python 上也能运行，
并说明在当前操作系统上如何修复发现的问题。然后脚本只在依赖有变化时才安装，并一直运行到按下 Ctrl+C。
自己运行 `python scripts/doctor.py` 可以查看完整报告。

### 各操作系统的前置条件

| | Python 3.11+ | Node.js 22+（仅界面开发） | Azure CLI（可选） |
| --- | --- | --- | --- |
| Windows | `winget install -e --id Python.Python.3.13` | `winget install -e --id OpenJS.NodeJS.LTS` | `winget install -e --id Microsoft.AzureCLI` |
| macOS | `brew install python@3.13`（macOS 自带 3.9） | `brew install node` | `brew install azure-cli` |
| Ubuntu、Debian | `sudo apt install python3 python3-venv`；Ubuntu 22.04 / Debian 11 上为 `python3.11 python3.11-venv` | [nodejs.org](https://nodejs.org/en/download)（apt 中的版本较旧） | `curl -sL https://aka.ms/InstallAzureCLIDeb \| sudo bash` |
| Fedora、RHEL | `sudo dnf install python3.12` | [nodejs.org](https://nodejs.org/en/download) | [安装指南](https://learn.microsoft.com/cli/azure/install-azure-cli) |

在 Windows 上，如果 `python` 打开了 Microsoft Store，请在*设置 > 应用 > 高级应用设置 > 应用执行别名*中关闭该别名。
脚本会先查找 `python3.13` … `python3.11`，再查找 `python3`，因此能找到与系统 Python 并存的较新版本。

### 登录

应用按以下顺序尝试：在设置页上完成的登录，然后是 Azure CLI（`az login`）、Azure Developer CLI（`azd auth login`）
和 Azure PowerShell（`Connect-AzAccount`）。未安装或未登录的工具会被跳过一分钟，而不是在每个请求上重试。

- **用浏览器登录**会在本机打开 Microsoft 登录页面。
- **改用验证码登录**适用于没有浏览器的机器：SSH、Linux 服务器、容器，或浏览器无法打开的 WSL。在任意设备上打开
  https://microsoft.com/devicelogin 并输入显示的验证码。
- 在应用中完成的登录会在重启后**保留**，保存在操作系统的加密存储中（Windows 上为 DPAPI，macOS 上为钥匙串，
  Linux 上为 libsecret）。`backend/.pool-manager-auth.json` 只保存帐户记录。在无法加密的环境中（没有密钥环的 Linux 或
  WSL），应用会把登录状态和连接的网关的主密钥保存在内存中并给出提示；它从不以明文写入它们。**退出此登录**会清除登录。

### 网络代理与证书

- **代理：** 设置 `HTTPS_PROXY=http://proxy:8080`（以及 `NO_PROXY=localhost,127.0.0.1`）。应用、pip 和 npm 都会读取它。
- **TLS 检查**（公司证书）：把公司 CA 安装到操作系统证书存储中。在本地，应用会使用该存储验证 TLS
  （[truststore](https://pypi.org/project/truststore/)；`POOL_MANAGER_SYSTEM_TRUST=false` 可关闭）。pip 24.2+ 也是如此。
  对于较旧的 pip，设置 `PIP_CERT=<ca.pem>`；对于 npm，设置 `NODE_EXTRA_CA_CERTS=<ca.pem>`。
- **软件包镜像：** `PIP_INDEX_URL=https://<mirror>/simple/` 和 `npm_config_registry=https://<mirror>/`。doctor 会检查镜像，
  而不是 PyPI 和 npm。
- 应用需要访问 `https://login.microsoftonline.com` 和 `https://management.azure.com`，还需要访问你的网关 URL（测试
  调用，以及 LiteLLM 的管理 API）、用于 LiteLLM 网关指标监控选项卡的 `https://api.loganalytics.azure.com`，以及用于
  标价的 `https://prices.azure.com`。doctor 能区分 DNS 失败、超时、不受信任的证书以及要求凭据的代理。

### 远程机器和 WSL

通过 SSH 时，用验证码登录，并转发端口以在你自己的电脑上打开界面：`ssh -L 8000:localhost:8000 <host>`
（使用开发服务器时为 `5174`）。在 WSL 中，请在 WSL 内用 Linux 版 Python 运行 `scripts/start-local.sh`；
`.gitattributes` 会让 shell 脚本即使在 Windows 检出中也保持 LF 换行符。

### 手动运行

```sh
cd backend
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt   # Linux/macOS 上为 bin/pip
cp .env.example .env                       # 无需修改；在设置页上选择网关
.venv/Scripts/python -m uvicorn pool_manager.app:app --reload           # 如果已构建，会提供 static/

cd ../frontend && npm ci && npm run dev    # http://localhost:5174，把 /api 代理到 :8000
```

在克隆的仓库中运行 `npm run build` 后，`python scripts/package-release.py` 会构建发布 zip。推送 `v*` 标签会发布一个
（`.github/workflows/release.yml`）。

修改 `infra/gateway/litellm-aca.bicep` 或 `litellm-aks.bicep` 后，请运行 `python scripts/build-gateway-template.py`
（需要带 Bicep 的 Azure CLI）重新编译 `backend/pool_manager/templates/litellm-{aca,aks}.json`，应用部署的就是这些
文件。CI 会用 `--check` 运行它。

> [!CAUTION]
> 本地开发使用**你**的 Azure 身份，作用于你**真实**的网关。所有更改立即生效。

## 测试

```sh
cd backend && .venv/Scripts/pip install pytest==9.1.1 ruff==0.15.22 mypy==2.3.0
.venv/Scripts/python -m pytest -q && .venv/Scripts/python -m ruff check . && .venv/Scripts/python -m mypy
cd ../frontend && npm test && npm run build
```

后端测试使用一个内存中的 Azure（`tests/fake_azure.py`：ARM、模板部署、权限和角色、Foundry、Azure Monitor、
Log Analytics、成本管理和零售价格）、构建在它之上的内存中的 APIM（`tests/apim/fake_apim.py`，它会强制执行 ETag
以及 `If-Match` / `If-None-Match`），以及一个内存中的 LiteLLM 代理（`tests/litellm/fake_litellm.py`，它的管理 API
和聊天补全，按 v1.106 的行为建模）。不需要 Azure 访问。

另有一个可选的集成测试，让 LiteLLM 服务层在 Docker 中对接一个**真实**的 LiteLLM 代理，带有 PostgreSQL 和两个模拟的
Azure OpenAI 区域（`tests/litellm_stack/`）。它检查 LiteLLM 是否接受应用写入的行、凭据、别名和密钥，以及是否按
后端池的设置路由：

```sh
cd backend
docker compose -f tests/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
```
