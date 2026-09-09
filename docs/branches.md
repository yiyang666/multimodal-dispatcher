# 分支策略：main 与 sleep_patch

## main（默认）

适用于 **vLLM Sleep Mode 可正常工作** 的环境（原生 Linux + 正常 CUDA VMM）：

- `deploy/vllm-compose.yaml` 启用 `--enable-sleep-mode` 与 `VLLM_SERVER_DEV_MODE`
- Dispatcher 通过 `/is_sleeping` 判断就绪，空闲走 `/sleep`，请求走 `/wake_up`
- **不包含** WSL CuMem 二进制补丁

部署前请实测：`POST /sleep` → 显存下降 → `POST /wake_up` → 对话正常。

## sleep_patch

适用于 Sleep Mode **会报错** 的环境，典型为 **WSL2**：驱动谎报
`GPU_DIRECT_RDMA_WITH_CUDA_VMM_SUPPORTED`，随后 `cuMemSetAccess` 返回 `CUDA_ERROR_UNKNOWN`。

本分支在 main 基础上增加：

- `deploy/vllm-patches/cumem_allocator.*`（强制跳过 RDMA capable flag）
- compose 只读挂载覆盖镜像内 `cumem_allocator.abi3.so`

```bash
git fetch origin
git checkout sleep_patch
# 按 docs/deployment.md 部署；确保 patch 挂载路径有效
```

上游驱动或 vLLM 修好后，切回 `main` 并去掉补丁挂载即可。

## 不要做的事

- 不要把 `sleep_patch` 的 `.so` 合进 `main`（污染干净部署路径）
- 不要在未验证平台上只改 `sleep_supported: true` 却不启用 sleep_patch
