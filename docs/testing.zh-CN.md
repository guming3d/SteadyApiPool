# 测试

[English](testing.md)

测试分四层，从最快到最真实。前三层在每个拉取请求以及每次推送到 `main` 和 `unified` 时运行
（`.github/workflows/ci.yml`）；实时 Azure 旅程每晚、每周或手动运行（`.github/workflows/e2e-azure.yml`）。

| 层 | 验证什么 | 在哪里运行 | 时间 | 费用 |
| --- | --- | --- | --- | --- |
| 单元和 API 测试（`backend/tests`、`frontend/tests`） | 每个路由、规则和不变量，针对内存中的 Azure、APIM 和 LiteLLM 模拟实现 | CI、本地 | 约 1 分钟 | 无 |
| 1. 浏览器旅程（`e2e/browser`） | 真实的界面，像用户一样逐步点击，后端是连接到模拟实现的真实后端 | CI、本地 | 约 1 分钟 | 无 |
| 2. 真实 LiteLLM（`backend/tests/litellm/test_litellm_stack*.py`） | 应用的 API 和服务层，针对固定版本的 LiteLLM 映像、Postgres 和两个可以故障的模拟区域 | CI、装有 Docker 的本地 | 约 1 分钟 | 无 |
| 3. 实时 Azure（`e2e/azure`） | 用户在真实 Azure 上的完整首次使用：部署网关、设置、Foundry、后端池、故障转移、监视、费用、调整规模、删除 | 按计划运行的 GitHub Actions、本地 | 每个旅程 25-60 分钟 | 见下文 |

## 单元和 API 测试

```sh
cd backend && .venv/Scripts/python -m pytest -q      # macOS 和 Linux 上为 bin/python
cd frontend && npm test
```

模拟实现和约定见 [development.zh-CN.md](development.zh-CN.md)。

## 1. 浏览器旅程

Playwright 用 Chromium 操作由 `scripts/demo_server.py` 提供的已构建界面；该服务器运行真实的 FastAPI
应用，连接内存中的 Azure、APIM 服务和 LiteLLM 网关。每个测试先把演示重置到一个已知阶段
（`POST /demo/stage/{sign-in,ready,pool}`），因此测试互不依赖。

```sh
cd frontend && npm ci && npm run build && cd ..
cd e2e/browser
npm ci && npm run install-browsers
npm test                 # 用 npm run test:headed 观看；失败后用 npx playwright show-report
```

| 测试 | 旅程 |
| --- | --- |
| `onboarding`（LiteLLM、APIM） | 未登录、登录、选择网关类型、选择现有网关、进入后端池页面 |
| `litellm-gateway`：部署 | **部署网关**：规模计算、区域检查、部署进度、应用打开新网关 |
| `litellm-gateway`：面板 | 费用、**按新的流量调整规模**（更改列表和确认）、**切换或创建** |
| `litellm-gateway`：指标 | 后端池的指标选项卡和网关指标选项卡 |
| `litellm-pools`：创建 | 从模型创建后端池、故障转移顺序、审阅并发布、授予 Foundry 访问权限、测试密钥和测试调用（显示响应的API节点） |
| `litellm-pools`：编辑和删除 | 修改冷却和重试并在重新加载后读回，然后按删除计划删除后端池 |
| `apim`：创建 | 带 API 的后端池、策略视图、测试密钥和测试调用 |
| `apim`：使用中 | 指标、网关负载、费用、切换 |
| `shell` | 浅色和深色模式、语言切换，重新加载后都保持 |

需要后端的虚拟环境（`backend/.venv`），或用 `E2E_PYTHON` 指向安装了 `backend/requirements.txt` 的
Python；`E2E_PORT` 可以把演示服务器从 8010 移到别的端口。失败时，CI 会把 HTML 报告、跟踪、屏幕截图和视频
保存为制品。

## 2. 真实 LiteLLM

```sh
cd backend
docker compose -f tests/litellm/litellm_stack/docker-compose.yml up -d --wait
.venv/Scripts/python -m pytest -q -m litellm
docker compose -f tests/litellm/litellm_stack/docker-compose.yml down -v
```

这套环境包括 LiteLLM（与应用部署的映像相同、配置相同）、它的 Postgres，以及两个模拟的 Azure OpenAI
区域，它们的响应可以通过 `POST /control` 更改（200、429、500）。这些测试检查只有真实代理才能体现的内容：

- `test_litellm_stack.py`：应用写入的行、凭据、别名和密钥都被接受；按 `order` 故障转移、冷却、密钥范围、
  `x-litellm-model-id`；以及使用方无法把后端池的调用发往其他主机（`api_base`、`base_url`、
  `azure_ad_token`，也包括嵌套在 `extra_body` 或 `metadata` 中的），否则在启用
  `enable_azure_ad_token_refresh` 时会把网关的令牌带过去。
- `test_litellm_stack_app.py`：用户通过应用 HTTP API 的旅程，与浏览器发出的请求相同：API节点、编辑器的
  预览、后端池、别名、测试密钥和调用、故障转移、编辑（以及拒绝过期的 ETag）、性能测试、CSRF 检查、删除；
  以及清理只删除本应用创建的内容。

每个测试开始时都会通过 LiteLLM 的管理 API 清除所有名为 `it-*` 的内容，因此一次失败的运行不会让下一次失败。

## 3. 实时 Azure

每个旅程是一个由有序步骤组成的模块，由从 `backend/pool_manager` 的临时副本启动的应用运行（它有自己的状态
文件和密钥存储，从不使用检出目录中的），以 Azure CLI 的身份登录。步骤只调用应用的 HTTP API，与界面完全
相同，另外用 Azure CLI 创建本次运行的资源组并检查应用创建的内容。某一步失败时，该旅程的其余步骤会被跳过；
无论发生什么，拆除阶段都会删除资源组。

| 旅程（`e2e/azure/`） | 步骤 | 时间 | 每次费用 |
| --- | --- | --- | --- |
| `test_litellm_aca.py` | 首次运行、规模计算和区域检查、在容器应用上部署试用网关、检查部署结果（HTTPS 源、排空、入口）、设置授予 Foundry 访问权限、发现和导入、第一个API节点无法响应的后端池、故障转移到 Foundry 的测试调用、别名、性能测试、指标监控和网关指标选项卡、费用、不做任何更改的调整规模计划、删除后端池、删除网关 | 约 25 分钟 | 不到 1 美元 |
| `test_apim.py` | 首次运行、创建 BasicV2 服务、启用其标识并授予 Foundry 访问权限、API 背后的后端池（策略带有本应用的标记）、密钥和越过无法响应的API节点的测试调用（断路器）、性能测试、指标监控和网关指标、费用、删除后端池（API 随之删除）、软删除服务（拆除阶段会清除） | 约 30-60 分钟 | 约 1 美元 |
| `test_litellm_aks.py` | 首次运行、AKS 规模和区域检查、部署群集、通过运行命令安装并通过 Let's Encrypt 启用 HTTPS、群集设置以及部署身份的管理员角色、设置、通过群集 HTTPS 地址响应并故障转移的后端池、调整规模计划、删除 | 45-60 分钟 | 几美元 |

每个后端池有三个API节点：第一个是主机名无法解析的终结点，后面是两个长期存在的 Foundry 部署。因此每次调用都
会真正故障转移，又不会破坏真实的部署：LiteLLM 在同一次调用中改用下一个API节点；APIM 的断路器在第一次失败
时跳变，之后的调用发往下一个API节点。

### 一次性准备：Foundry 帐户和 GitHub 身份

以有权创建资源和应用注册的身份 `az login` 后：

```sh
python scripts/e2e-azure-setup.py --dry-run foundry     # 将要执行的操作
python scripts/e2e-azure-setup.py foundry               # 两个帐户，各有一个 gpt-4.1-mini
python scripts/e2e-azure-setup.py identity --repo <owner>/<repo>
```

`foundry` 创建资源组 `e2e-foundry`，在两个区域（`--regions`，默认 `eastus2 swedencentral`）各创建一个
AI 服务帐户，每个帐户有一个同一模型（`--model`、`--version`）的全局标准部署 `e2e-chat`。按令牌付费：
空闲时不产生费用。它会输出 `E2E_SUBSCRIPTION_ID`、`E2E_FOUNDRY_ACCOUNTS` 和 `E2E_FOUNDRY_DEPLOYMENT`。

`identity` 创建 GitHub Actions 用来登录的应用注册，没有机密：为存储库的 `azure-e2e` 环境添加联合凭据。
它在订阅中获得*参与者*角色，以及受条件限制的*基于角色的访问控制管理员*角色，只能分配本应用会分配的两个角色
（*认知服务 OpenAI 用户*、*Azure Kubernetes 服务 RBAC 群集管理员*），因此无法授予其他任何权限。它会输出
`AZURE_CLIENT_ID`、`AZURE_TENANT_ID` 和 `AZURE_SUBSCRIPTION_ID`。

然后在 GitHub 中：**Settings > Environments > New environment** 创建 `azure-e2e`。限制它的部署分支（例如
`main` 和 `unified`），使拉取请求无法运行它，并把上面的六个值添加为环境**变量**（都不是机密）。可以另外
设置 `E2E_LOCATION`（默认 `swedencentral`）和 `E2E_GATEWAY_SIZE`。

**订阅可以部署到哪里。** 各订阅不同：有些订阅在某个区域根本不能创建 PostgreSQL（应用的区域检查会在创建任何资源之前
指出，LiteLLM 旅程随后改用它给出的第一个区域）；有些订阅即使区域列出了*可突发*规格也不能创建（Azure 只在创建服务器时
返回 `SkuNotAvailable`，任何只读检查都看不出来）。对这类订阅，请设置 `E2E_GATEWAY_SIZE=small`：LiteLLM 旅程会部署
最多两个副本、常规用途数据库和 Redis，而不是单副本的试用网关（`trial`，默认值）；每次运行的费用略高，同时也覆盖了
生产路径。

### 运行

在 GitHub 中：**Actions > E2E (live Azure) > Run workflow**，选择旅程、区域，以及是否保留资源。它也会按
计划自动运行：每晚运行容器应用和 APIM，每周日运行 AKS；每个旅程是单独的作业，同一旅程一次只运行一个。

在本地，以 `az login` 登录后：

```sh
export E2E_LIVE=1 E2E_SUBSCRIPTION_ID=<id> E2E_FOUNDRY_ACCOUNTS=<id>,<id> E2E_FOUNDRY_DEPLOYMENT=e2e-chat
# 可选：E2E_TENANT_ID、E2E_LOCATION、E2E_GATEWAY_SIZE=small，以及 E2E_KEEP=1 保留资源以便查看
cd backend && .venv/Scripts/python -m pytest ../e2e/azure/test_litellm_aca.py
```

没有 `E2E_LIVE=1` 时，所有步骤都会被跳过，不会意外创建任何资源（CI 在每次推送时都以这种方式收集它们）。

### 清理

每次运行会在拆除阶段删除自己的资源组（`e2e-<旅程>-<运行>`），并清除它软删除的 APIM 服务。如果运行被取消
或运行器中断，工作流的 janitor 作业以及手动运行的 `python scripts/e2e-azure-janitor.py --subscription <id>
[--dry-run]` 会删除带有 `steadyapipool-e2e` 标记且超过六小时的资源组，并清除名为 `e2eapim…` 的已删除
APIM 服务。它从不触碰其他任何内容，包括 `e2e-foundry`。

### 一小时以上的登录

应用从 Azure CLI 获取每个 Azure 令牌。在 GitHub Actions 中，CLI 的联合登录只在工作流的 OIDC 令牌有效期内
（几分钟）有效，而一个旅程需要一小时。因此测试框架（`harness.AzureLogin`）每四分钟向 GitHub 申请一个新的
OIDC 令牌并重新登录 CLI；应用在下一次申请令牌时就会使用它。在本地则直接使用你自己的 `az login`。

## 添加测试

- 界面更改：在 `e2e/browser/tests` 中添加或修改旅程，按角色、标签和用户看到的英文文本选择元素（不使用测试
  ID）。每个测试都从一个阶段开始。
- 应用写入 LiteLLM 的内容有变化：在 `test_litellm_stack_app.py`（通过应用的 API）或
  `test_litellm_stack.py`（服务层）中添加一步。
- Azure 上新的用户操作：在所属旅程中添加一步，通过应用的 API 执行，并用 Azure CLI 检查它改变了什么。等待
  Azure 的步骤使用 `Api.wait`，带超时和 `failed` 原因，绝不使用固定的等待时间。
