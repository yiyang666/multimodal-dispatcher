# 架构方案

## 目标

在单 GPU 上给 OpenClaw 提供稳定的 `:8000` OpenAI 兼容入口，让文本、图像、视频模型按需获得 GPU；非活动后端释放显存，而不是争抢显存。

## 分层

```text
OpenClaw / 其他客户端
        │  Tailscale 私网 :8000
        ▼
Dispatcher
  ├── 模型目录与能力声明
  ├── GPU 全局互斥锁
  ├── 后端唤醒 / 休眠 / 健康检查
  └── 协议转发与任务状态
        │ 专用 Docker 网络 llm-backplane
        ├── vLLM Agent :8101
        ├── vLLM Coder :8102
        ├── ComfyUI Image :8188
        └── ComfyUI Video :8189
```

## 生命周期

1. 请求到达后按 `model` 字段定位后端。
2. 获取全局 GPU 锁，避免并发切换。
3. 让当前后端进入空闲：支持时 vLLM 使用 `/sleep?level=1`，否则停止容器；ComfyUI 优先 `/free`。
4. 唤醒或启动目标容器，并轮询健康接口。
5. 转发原始请求，流式响应透传。
6. 最后一个请求完成后开始空闲计时；达到阈值且没有在途请求时，自动释放当前后端。

vLLM Sleep Level 1 会将权重卸载到 CPU 内存并清除 KV cache。它适合在 Agent/Coder 之间频繁切换；大模型会占用相应 CPU 内存。当前 6000 Ada WSL 的 CUDA 虚拟内存分配器无法初始化 Sleep Mode，因此模型目录将 `sleep_supported` 设为 `false`，切换时停止 vLLM 容器。以后驱动/WSL/vLLM 组合验证通过后可重新开启。图片与视频模型通常更适合 ComfyUI 常驻进程加 `/free`，空闲很久后再停止容器。

## 配置模型

`config/models.yaml` 是唯一模型目录。未来新增模型时：

1. 在后端 Compose 文件增加容器，端口仅发布到 `127.0.0.1`。
2. 在目录中登记 `id`、`kind`、容器名、内部 URL、健康检查和能力。
3. 先创建但不启动容器。
4. 将 `enabled` 改为 `true`。

## 安全

- Dispatcher 是唯一暴露至 Tailscale 的服务。
- 后端不得暴露到 `0.0.0.0` 或公网。
- `/admin/*` 必须携带 `X-Dispatcher-Token`。
- Docker socket 只挂载给 Dispatcher；它是受信任控制平面，不能对公网开放。
