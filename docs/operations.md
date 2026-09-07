# 部署与运维手册

## 初次部署

```bash
cd ~/llm-stack/dispatcher
cp .env.example .env
openssl rand -hex 32
# 将输出填入 DISPATCHER_ADMIN_TOKEN
cp config/models.example.yaml config/models.yaml
docker compose up -d --build
```

## 当前 Agent 后端

Agent 后端加入专用 Docker 网络 `llm-backplane`，仅保留宿主机回环端口 `8101` 用于诊断，并加入：

```text
--max-num-seqs 128
```

`--max-num-seqs 128` 是本机 Qwen3.8-27B-FP8 的安全值。该模型在 48GB 显存、32768 上下文、0.90 显存利用率下无法支持 vLLM 默认的 256 并发序列。

`~/.cache/vllm` 必须挂载到容器内同一路径，以持久化 Torch/Triton 编译缓存。第一次启动仍会编译，后续容器切换和重启会明显更快。

当前 WSL 环境启用 Sleep Mode 会在 CUDA 虚拟内存分配器初始化时失败，因此生产配置暂时禁用 Sleep Mode。Dispatcher 在切换文本模型时停止旧容器，并在需要时重新启动。ComfyUI仍通过 `/free` 释放模型显存。

## 空闲释放

`.env` 默认配置如下：

```dotenv
IDLE_TIMEOUT_SECONDS=600
IDLE_CHECK_INTERVAL_SECONDS=30
```

最后一个普通或流式请求结束 10 分钟后，如果没有其他在途请求，Dispatcher
自动释放活动后端。当前 vLLM 容器会被停止；ComfyUI 容器保持运行并调用
`/free` 卸载模型。将超时设为 `0` 可关闭该机制。修改后执行：

```bash
docker compose up -d
```

## 健康检查

```bash
curl http://100.66.123.42:8000/health
curl http://100.66.123.42:8000/v1/models
curl -H "X-Dispatcher-Token: $DISPATCHER_ADMIN_TOKEN" \
  http://100.66.123.42:8000/admin/status
```

Dispatcher 只监听配置的 Tailscale 地址，因此从服务器本机检查时也应使用
该地址，而不是 `127.0.0.1:8000`。

## 常用故障定位

```bash
docker logs -f llm-dispatcher
docker logs -f agent-6000ada
/usr/lib/wsl/lib/nvidia-smi
```

如果 `nvidia-smi` 找不到命令，将 `/usr/lib/wsl/lib` 加入 `PATH`：

```bash
export PATH="/usr/lib/wsl/lib:$PATH"
```

## 模型切换

OpenClaw 只需向稳定入口发起请求并填写正确模型 ID。Dispatcher 会完成后端切换。管理员也可手动预热：

```bash
curl -X POST \
  -H "X-Dispatcher-Token: $DISPATCHER_ADMIN_TOKEN" \
  http://100.66.123.42:8000/admin/activate/agent-6000ada
```

图片和视频模型确定前，`image-6000ada`、`video-6000ada` 保持禁用；不能为了占位而启动空容器。
