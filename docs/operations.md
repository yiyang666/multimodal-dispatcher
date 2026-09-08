# 运维手册

初次安装请先阅读 [通用部署指南](deployment.md)，命令行操作见 [`modelctl` 使用手册](cli.md)。

## 日常状态检查

```bash
modelctl health
modelctl list
modelctl status
```

`health` 只说明 Dispatcher 进程可访问；`status` 才会检查各容器是否运行及后端健康接口是否
就绪。

查看日志：

```bash
modelctl logs dispatcher --tail 200
modelctl logs agent-6000ada -f
modelctl logs image-6000ada -f
```

## 模型切换和显存让渡

业务请求会根据 `model` 字段自动切换。管理员需要提前预热时使用：

```bash
modelctl use agent-6000ada
```

需要立即把 GPU 让给其他用户时：

```bash
modelctl release
```

正在执行任务时，手动释放会被拒绝而不是中断任务。不要用 `docker compose stop` 模拟日常切换，
因为 Dispatcher 无法把这种外部动作纳入在途任务保护。

## 空闲释放

`.env` 中的默认配置：

```dotenv
IDLE_TIMEOUT_SECONDS=600
IDLE_CHECK_INTERVAL_SECONDS=30
SWITCH_WAIT_TIMEOUT_SECONDS=900
```

- 最后一个普通或流式请求结束后开始计算空闲时间。
- 到达阈值且没有在途请求时自动释放活动后端。
- vLLM 根据 `sleep_supported` 选择休眠或停止容器。
- ComfyUI 调用 `/free`；容器继续显示 `Up` 是正常现象。
- 跨模型请求会等待当前任务完成，超过切换等待阈值则返回 503。

修改后重建 Dispatcher 容器使环境变量生效：

```bash
docker compose up -d
modelctl status
```

将 `IDLE_TIMEOUT_SECONDS` 设为 `0` 可以关闭自动释放。

## 更新

更新前先查看本地修改：

```bash
git status --short
git pull --ff-only
docker compose up -d --build
```

只重建 Dispatcher 不会重新下载模型，也不会删除后端容器。更新涉及工作流模板时，应同时确认
`docker-compose.yaml` 已挂载对应文件。

## 新增模型

1. 准备模型权重和后端镜像。
2. 创建后端容器并加入 `llm-backplane`。
3. 使用仅回环的诊断端口验证后端健康。
4. 在 `config/models.yaml` 增加稳定模型 ID。
5. 将 `enabled` 设为 `true`。
6. 重启 Dispatcher 并执行 `modelctl list`、`modelctl use <ID>` 验收。

不要复用同一容器名表示不同模型。模型 ID 是客户端契约，替换底层模型时应记录变更并考虑创建
新的 ID。

## 常见故障

### `nvidia-smi: command not found`（WSL）

WSL 工具通常位于 `/usr/lib/wsl/lib`：

```bash
export PATH="/usr/lib/wsl/lib:$PATH"
```

将该行写入实际 Shell 会读取的启动文件，例如交互 Bash 使用 `~/.bashrc`。变量名 `PATH` 必须
大写。

### 后端容器不存在

Dispatcher 返回 `Container ... is not created` 时，使用后端 Compose 执行 `create`，并确认
注册表中的 `container` 与 `docker ps -a` 显示的名称完全一致。

### 后端启动后长时间未就绪

```bash
modelctl logs <模型ID> -f
nvidia-smi
docker inspect <容器名>
```

常见原因包括显存不足、模型文件不完整、vLLM 上下文或并发设置过大、工作流节点缺失，以及
CUDA/驱动版本不匹配。

### ComfyUI 容器运行但显存很低

如果 `modelctl status` 显示 ComfyUI 为 `idle`，这通常表示空闲策略已经调用 `/free`。下一次
图片请求会重新加载权重，不需要重启容器。

### vLLM Sleep Mode 启动失败

将对应模型的 `sleep_supported` 改为 `false`。Dispatcher 会停止容器来完整释放显存，代价是
下一次请求需要冷启动。WSL 环境尤其应先实测再打开 Sleep Mode。

### 手动释放返回 409

说明仍有业务请求执行。等待 `modelctl status` 中 `In flight` 变为 0 后重试。除非确认可以丢失
任务，否则不要绕过 Dispatcher 强制停止容器。

## 备份范围

建议备份：

- 实际 `config/models.yaml`
- `.env`（按机密文件管理）
- 后端 Compose 文件
- 自定义 ComfyUI API 工作流

模型权重通常可以重新下载，可根据带宽和恢复时间目标决定是否备份。输出图片、输入素材与用户
数据不属于 Dispatcher，应按后端自己的数据策略备份。
