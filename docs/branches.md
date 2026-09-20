# 开发、发布与部署链路

本项目采用“**开发机是唯一源码来源，服务器是固定版本的部署消费者**”的模式。公共仓库负责完整
功能、测试、通用工作流和脱敏配置样例；部署服务器只保存与机器相关的私有配置，不在服务器上
开发、修改或提交源码。

## 边界与单一事实来源

| 内容 | 开发机 Git 仓库 | 部署服务器 |
|------|----------------|------------|
| `src/`、测试、Dockerfile、通用 Compose | 开发、评审并提交 | 只从发布 Tag 读取，禁止直接修改 |
| `deploy/*.json` 通用工作流 | 脱敏后提交 | 随 Tag 部署 |
| `config/models.example.yaml`、`.env.example` | 提供无真实地址的样例 | 不直接作为真实配置使用 |
| `.env`、`config/models.yaml` | 不提交 | 按机器维护，必须被 Git 忽略 |
| `docker-compose.override.yaml` | 不提交机器专用版本 | 可用于端口、挂载和路径等环境差异 |
| 模型权重、输入输出和缓存 | 不进入源码仓库 | 放在服务器持久化目录 |

真实 IP、域名、令牌、用户名、绝对数据路径和内部模型信息不得写入已跟踪文件。若一项差异会改变
Dispatcher 的通用行为，它就是源码功能，必须先回到开发机实现；不能把服务器上的源码补丁当作
“环境配置”。

## 分支

只维护一条开发线：**`develop`**。

| 分支 | 用途 |
|------|------|
| `develop` | 日常开发：新 kind / model 字段、workflow、文档、可选补丁资产 |
| `main` | 稳定发布镜像（按需从 develop 合并）；不要在 main 上堆实验 |
| `sleep_patch` | **已废弃**。原 WSL CuMem 补丁内容已并入 `develop` 的 `deploy/vllm-patches/` |

生产机（如 5090）是 **消费者**：不在服务器仓库修改、提交或推送源码。开发机完成改动、测试和
提交后创建发布 Tag，生产机只检出该 Tag 并构建 Docker 服务。

```bash
git fetch origin
git checkout develop
git pull --ff-only origin develop   # 若已推送
```

## 标准开发与发布流程

### 1. 开发机实现并验证

在 `develop` 上修改源码、测试、通用 Compose、工作流和脱敏样例：

```bash
git checkout develop
git pull --ff-only origin develop
# 修改并运行项目测试/验收
git status --short
git add <明确的文件>
git commit -m "..."
git push origin develop
```

不得从生产服务器复制包含真实 IP、令牌或绝对路径的配置到提交中。服务器上临时出现的通用功能
修补，必须先人工审查并移植回开发机，再经过同样的测试和提交过程。

### 2. 创建不可变发布 Tag

将已验证提交合入稳定发布线，并在开发机创建带注释的 Tag。下面的版本号只是示例：

```bash
git checkout main
git pull --ff-only origin main
git merge --ff-only develop
git tag -a v0.4.0 -m "Release v0.4.0"
git push origin main v0.4.0
```

如果仓库的发布策略需要 Pull Request，则在合并完成后对 `main` 上的最终提交打 Tag。已推送的 Tag
视为不可变；修复应发布新 Tag，不移动或覆盖旧 Tag。

### 3. 服务器检出 Tag 并构建

部署前必须确认已跟踪文件没有本地修改：

```bash
git status --short
git diff --quiet
git diff --cached --quiet
```

`.env`、`config/models.yaml` 和 `docker-compose.override.yaml` 被忽略时可以保留。若 `git status`
仍显示 `src/`、Dockerfile、已跟踪 Compose 或工作流发生变化，停止部署：先将通用能力移植回开发机，
不要在服务器提交，也不要用 `reset --hard` 掩盖未审查的功能。

工作树满足要求后再部署固定版本：

```bash
git fetch --tags origin
git checkout --detach v0.4.0
docker compose config --quiet
docker compose up -d --build
./modelctl health
./modelctl list
./modelctl status
```

服务器不使用普通 `git pull` 跟随 `develop`。升级就是检出一个新的发布 Tag；回滚则检出上一个
已知正常的 Tag，再执行相同的 Compose 构建与验收命令。

## 允许的服务器环境差异

服务器可以根据硬件和网络环境调整：

- `.env` 中的监听地址、管理令牌、超时等参数；
- `config/models.yaml` 中的实际模型 ID、内部后端地址、容器名和释放策略；
- `docker-compose.override.yaml` 中的端口、卷挂载、设备和环境变量；
- 仓库外的模型权重、缓存、输入输出目录及后端容器配置。

这些文件应备份，但不产生项目提交。公共仓库中的示例必须使用占位值或容器 DNS 名称，不包含
任何生产机真实 IP 和秘密。

## Sleep Mode 不是分支，是 vLLM 配置

Sleep 只对 **`kind: vllm`** 有意义。ComfyUI / Ollama / llama.cpp 用各自的 `release` 策略，不要为它们开 `sleep_supported`。llama.cpp 默认停容器。

| 场景 | 做法 |
|------|------|
| 原生 Linux，Sleep 实测可用 | compose 开 `--enable-sleep-mode`；`sleep_supported: true` |
| WSL2 CuMem 报错 | 仍用 **同一 develop**；挂载 `deploy/vllm-patches/cumem_allocator.abi3.so`（见该目录 README），再开 `sleep_supported: true` |
| Sleep 不可用或不想用 | `sleep_supported: false`（空闲时 docker stop） |

验证：`POST /sleep` → 显存下降 → `POST /wake_up` → 对话正常。未验证前不要开 `sleep_supported: true`。

## 新增模型 / workflow（本机 → 生产）

1. 开发机 `develop`：实现通用能力、增加 `deploy/*.json`、测试并更新脱敏的样例和文档。
2. 创建并推送新的发布 Tag。
3. 生产服务器检出该 Tag，保留其私有配置并重建 Dispatcher 镜像。
4. 在服务器真实 `config/models.yaml` 登记 model ID；权重放到服务器持久化目录。
5. 运行健康检查和一次实际请求；失败时回滚到上一 Tag，而不是在服务器修改源码热修。

## 不要做的事

- 不要再检出或推送 `sleep_patch` 做功能开发
- 不要只靠改 YAML 开 Sleep，却不确认平台与补丁是否匹配
- 不要在生产机修改或提交 `src/`、Dockerfile、通用 Compose 和工作流
- 不要让生产机直接跟随 `develop`，也不要用无目标版本的 `git pull` 部署
- 不要把生产机当第二开发仓库长期分叉
