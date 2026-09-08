# `modelctl` 使用手册

`modelctl` 是 Multimodal Dispatcher 的官方本机管理 CLI。它是一个只依赖 Python 标准库的
脚本，通过 Dispatcher 管理 API 获取真实调度状态。除 `logs` 外，它不会直接启停 Docker，
因此不会绕过在途任务保护、模型互斥和空闲计时。

## 配置发现

CLI 按以下优先级读取地址和管理员令牌：

1. 命令行参数 `--url`、`--token`。
2. `DISPATCHER_URL`、`DISPATCHER_ADMIN_TOKEN` 环境变量。
3. `--env-file` 或 `MODELCTL_ENV_FILE` 指定的文件。
4. `modelctl` 所在项目目录的 `.env`。

检查最终配置时不会打印令牌本身：

```bash
modelctl config
```

远程管理示例：

```bash
DISPATCHER_URL=http://10.0.0.8:8000 \
DISPATCHER_ADMIN_TOKEN='your-token' \
modelctl status
```

管理员令牌属于敏感信息，不建议直接写进 Shell 历史；优先使用权限受控的 `.env` 文件。

## 常用命令

### 查看模型

```bash
modelctl list
```

有管理员令牌时会同时显示计划模型、容器和真实状态：

```text
MODEL            KIND     STATE    CONTAINER
agent-6000ada    vllm     stopped  agent-6000ada
image-6000ada    comfyui  idle     comfy-image-6000ada
video-6000ada    comfyui  planned  comfy-video-6000ada
```

状态含义：

- `active`：当前调度模型。
- `idle`：后端服务运行且健康，但不是当前活动模型；ComfyUI 可能已经 `/free`。
- `starting`：容器运行但健康检查尚未通过。
- `stopped`：容器已停止。
- `planned`：注册表中存在但尚未启用。

### 查看调度状态

```bash
modelctl status
```

它会显示活动模型、在途请求数、空闲时间、自动释放阈值及各后端状态。

### 切换或预热模型

```bash
modelctl use agent-6000ada
modelctl use image-6000ada
```

命令会一直等待到目标后端通过健康检查。若其他模型仍有任务执行，Dispatcher 会先等待任务
结束，再安全释放旧后端。冷启动可能需要数分钟。

为兼容旧脚本，以下命令等价：

```bash
modelctl start agent-6000ada
modelctl switch agent-6000ada
```

### 手动释放

释放当前活动模型：

```bash
modelctl release
```

释放指定模型：

```bash
modelctl release image-6000ada
```

存在在途请求时，手动释放会返回冲突错误，不会中断任务。vLLM 根据配置休眠或停止容器；
ComfyUI 调用 `/free` 并保留服务进程。旧命令 `modelctl stop` 是 `release` 的兼容别名。

### 查看日志

```bash
modelctl logs dispatcher
modelctl logs agent-6000ada --tail 200
modelctl logs image-6000ada -f
```

该命令在本机调用 `docker logs`，因此需要当前用户拥有 Docker 权限。按 `Ctrl+C` 退出持续日志。

### 健康检查

```bash
modelctl health
```

这里只检查 Dispatcher 进程。需要查看模型是否就绪时使用 `modelctl status`。

## 自动调用与手动管理的边界

业务客户端不需要调用 `modelctl use`。只要请求体中的 `model` 正确，Dispatcher 就会自动切换。
`modelctl` 主要用于管理员预热、显存让渡、部署验收和故障排查。

不建议再用 Docker Compose 手工实现“切换模型”。它会绕过 Dispatcher 状态，可能破坏模型互斥
或中断正在执行的任务。容器创建、升级和删除仍属于部署操作，可以继续使用 Compose。
