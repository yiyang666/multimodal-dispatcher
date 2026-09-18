# 开发与部署

## 分支

只维护一条开发线：**`develop`**。

| 分支 | 用途 |
|------|------|
| `develop` | 日常开发：新 kind / model 字段、workflow、文档、可选补丁资产 |
| `main` | 稳定发布镜像（按需从 develop 合并）；不要在 main 上堆实验 |
| `sleep_patch` | **已废弃**。原 WSL CuMem 补丁内容已并入 `develop` 的 `deploy/vllm-patches/` |

生产机（如 5090）是 **消费者**：不在服务器上长期改仓库。本机改 `develop` → 拷贝/部署到生产（workflow、`models.yaml`、镜像重建等）。

```bash
git fetch origin
git checkout develop
git pull --ff-only origin develop   # 若已推送
```

## Sleep Mode 不是分支，是 vLLM 配置

Sleep 只对 **`kind: vllm`** 有意义。ComfyUI / Ollama 用各自的 `release` 策略，不要为它们开 `sleep_supported`。

| 场景 | 做法 |
|------|------|
| 原生 Linux，Sleep 实测可用 | compose 开 `--enable-sleep-mode`；`sleep_supported: true` |
| WSL2 CuMem 报错 | 仍用 **同一 develop**；挂载 `deploy/vllm-patches/cumem_allocator.abi3.so`（见该目录 README），再开 `sleep_supported: true` |
| Sleep 不可用或不想用 | `sleep_supported: false`（空闲时 docker stop） |

验证：`POST /sleep` → 显存下降 → `POST /wake_up` → 对话正常。未验证前不要开 `sleep_supported: true`。

## 新增模型 / workflow（本机 → 生产）

1. 本机 `develop`：改代码 / 加 `deploy/*.json` / 更新 `config/models.example.yaml`
2. 生产：同步文件或重建 dispatcher 镜像；在真实 `config/models.yaml` 登记 model id
3. Comfy 权重放到生产机挂载目录（如 `checkpoints/sdxl/`），与 workflow 中路径一致

## 不要做的事

- 不要再检出或推送 `sleep_patch` 做功能开发
- 不要只靠改 YAML 开 Sleep，却不确认平台与补丁是否匹配
- 不要把生产机当第二开发仓库长期分叉
