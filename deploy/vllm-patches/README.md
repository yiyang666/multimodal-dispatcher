# WSL CuMem Sleep 补丁

## 问题
WSL 上报 `GPU_DIRECT_RDMA_WITH_CUDA_VMM_SUPPORTED=1`，vLLM CuMem 分配器会设置
`allocFlags.gpuDirectRDMACapable=1`，随后 `cuMemSetAccess` 失败：`CUDA_ERROR_UNKNOWN (999)`。

## 修复
`cumem_allocator.cpp` 强制跳过 RDMA capable flag，编译为 `cumem_allocator.abi3.so`，
由 `deploy/vllm-compose.yaml` 只读挂载覆盖官方镜像内同名文件。

## 重建（可选）

```bash
docker run --rm --gpus all -v "$PWD":/build --entrypoint bash vllm/vllm-openai:v0.28.0 -lc '
g++ -O2 -shared -fPIC -std=c++17 \
  -I/usr/include/python3.12 -I/usr/local/cuda/include -I/build \
  /build/cumem_allocator.cpp -o /build/cumem_allocator.abi3.so \
  -L/usr/local/cuda/lib64/stubs -lcuda
'
```

上游驱动或 vLLM 修好后，切回 `main` 并删除本挂载即可。
