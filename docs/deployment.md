# 通用部署指南

本文以一台 Linux 或 WSL2 单 GPU 服务器为例。示例模型可以替换，关键约束是：每个后端都是
一个预先创建的 Docker 容器，并与 Dispatcher 加入同一个 Docker 网络。

## 1. 前置条件

- Linux x86_64 或启用 WSL2 的 Windows。
- NVIDIA 驱动可用，宿主机执行 `nvidia-smi` 能看到 GPU。
- Docker Engine 24+ 与 Docker Compose v2。
- Docker 容器可以访问 GPU。可用下面的方式验证：

```bash
docker run --rm --gpus all nvidia/cuda:12.6.3-base-ubuntu24.04 nvidia-smi
docker compose version
```

原生 Linux 通常需要安装 NVIDIA Container Toolkit；Docker Desktop 的 WSL2 GPU 支持由
Windows 驱动和 Docker Desktop 提供。Dispatcher 本身不使用 GPU，但其后端容器需要。

## 2. 获取项目

```bash
git clone https://github.com/yiyang666/multimodal-dispatcher.git
cd multimodal-dispatcher
```

后续命令默认在项目根目录执行。

## 3. 创建私有后端网络

```bash
docker network inspect llm-backplane >/dev/null 2>&1 || \
  docker network create llm-backplane
```

Dispatcher 通过容器名访问后端，因此所有后端都必须加入这个网络。请勿把模型服务直接暴露到
公网。为了在宿主机排障，示例 Compose 只把端口绑定到 `127.0.0.1`。

## 4. 准备后端容器

Dispatcher 负责启动和停止已经存在的容器，但不会替你下载模型、生成 ComfyUI 工作流或创建
任意容器。仓库的 `deploy/` 提供两类可修改的示例。

### 4.1 vLLM 文本或视觉理解模型

编辑 `deploy/vllm-compose.yaml`：

- 将镜像版本改为你的 CUDA/驱动支持的 vLLM 版本。
- 将模型仓库或本地模型路径替换为自己的模型。
- `--served-model-name` 必须与 `config/models.yaml` 的对外 ID 一致。
- 根据显存调整上下文长度、并发序列数和显存利用率。
- 挂载 Hugging Face 与 vLLM 编译缓存，减少后续冷启动时间。

先拉取镜像并创建容器，不必让模型常驻：

```bash
docker compose -f deploy/vllm-compose.yaml pull
docker compose -f deploy/vllm-compose.yaml create
```

建议先手动启动一次完成下载和编译，再确认健康接口：

```bash
docker start agent-6000ada
docker logs -f agent-6000ada
curl http://127.0.0.1:8101/v1/models
docker stop agent-6000ada
```

不同模型的显存需求差异很大。示例参数只是 RTX 6000 Ada 48 GB 上的已知配置，不应直接当作
所有 GPU 的推荐值。

### 4.2 ComfyUI 图片或视频模型

编辑 `deploy/comfy-compose.yaml`，确认模型、输入、输出与工作流目录挂载正确。将模型权重放到
对应的 ComfyUI 目录，例如：

```text
models/
├── diffusion_models/
├── text_encoders/
├── vae/
└── loras/
```

然后拉取镜像并创建容器：

```bash
docker compose -f deploy/comfy-compose.yaml pull
docker compose -f deploy/comfy-compose.yaml create
docker start comfy-image-6000ada
curl http://127.0.0.1:8188/system_stats
```

仓库中的 Qwen-Image JSON 是已知节点编号的固定 API 工作流。换模型或修改 ComfyUI 工作流
后，需要同步修改模板及 `src/dispatcher/main.py` 中的输入节点映射。通用的任意工作流编译目前
不在项目范围内。

## 5. 配置模型注册表

```bash
cp config/models.example.yaml config/models.yaml
```

每个条目包含：

```yaml
models:
  my-agent:
    kind: vllm                 # vllm 或 comfyui
    enabled: true              # false 表示预留但不可调用
    container: my-agent        # 已创建的 Docker 容器名
    base_url: http://my-agent:8000
    readiness_path: /v1/models
    sleep_supported: false
    capabilities: [text]
```

注意事项：

- `base_url` 使用 Docker 网络内的容器名和容器端口，不是宿主机映射端口。
- vLLM 的 `sleep_supported` 只有在实际验证 Sleep Mode 可用后才能打开（见 `docs/branches.md`）。
- 默认 `main` 假定 Sleep 可用；WSL CuMem 失败时用 `sleep_patch` 分支，不要只改 YAML。
- ComfyUI 的释放策略固定优先调用 `/free`。
- `enabled: false` 的条目会出现在管理员状态中，但不会出现在 `/v1/models`。

## 6. 配置 Dispatcher

```bash
cp .env.example .env
openssl rand -hex 32
```

将生成的随机值写入 `.env`：

```dotenv
DISPATCHER_BIND_IP=127.0.0.1
DISPATCHER_ADMIN_TOKEN=替换为随机值
MODELS_FILE=/app/config/models.yaml
IDLE_TIMEOUT_SECONDS=600
IDLE_CHECK_INTERVAL_SECONDS=30
SWITCH_WAIT_TIMEOUT_SECONDS=900
```

- 仅本机使用时保持 `127.0.0.1`。
- 局域网或 Tailscale 使用时填写对应私网 IP，并使用防火墙限制来源。
- `IDLE_TIMEOUT_SECONDS=0` 会关闭自动释放。
- `SWITCH_WAIT_TIMEOUT_SECONDS` 是跨模型切换等待现有任务结束的最长时间。

不要提交 `.env` 和实际 `config/models.yaml`，它们已在 `.gitignore` 中排除。

## 7. 启动 Dispatcher

```bash
docker compose up -d --build
docker compose logs -f dispatcher
```

Dispatcher 容器挂载了 `/var/run/docker.sock`，因此能够启停模型容器。Docker socket 等同宿主机
Docker 管理权限，只能在可信服务器运行这个控制平面。

## 8. 安装并使用 CLI

可以直接从项目目录运行：

```bash
./modelctl health
./modelctl list
./modelctl status
```

也可以建立系统级命令链接：

```bash
sudo ln -s "$(pwd)/modelctl" /usr/local/bin/modelctl
modelctl status
```

CLI 默认读取项目根目录的 `.env`。如果脚本被复制到别处，使用
`MODELCTL_ENV_FILE=/path/to/.env` 指定配置。详见 [CLI 使用手册](cli.md)。

## 9. 验收

查看公开模型 ID：

```bash
./modelctl list
```

预热文本模型：

```bash
./modelctl use agent-6000ada
```

发送 OpenAI 风格请求：

```bash
curl http://127.0.0.1:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "agent-6000ada",
    "messages": [{"role": "user", "content": "Say hello"}],
    "stream": false
  }'
```

手动释放并确认状态：

```bash
./modelctl release
./modelctl status
```

## 10. 接入客户端

将 OpenClaw 或 OpenAI SDK 的 Base URL 指向 Dispatcher：

```text
http://<dispatcher-private-ip>:8000/v1
```

客户端仍按正常方式设置 `model`。业务请求会自动完成后端切换，不应在 Agent 提示词或业务
代码中执行 `docker`、`modelctl` 或 Compose 命令。

## 11. 更新与卸载

更新代码并重建 Dispatcher：

```bash
git pull --ff-only
docker compose up -d --build
```

停止 Dispatcher 不会删除模型文件或后端容器：

```bash
docker compose down
```

是否删除后端容器、镜像和权重应由管理员单独决定，Dispatcher 不执行这些破坏性操作。
