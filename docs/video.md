# 视频生成接口

视频任务通过 ComfyUI 固定 API 工作流异步执行。模型注册项必须使用 `kind: comfyui`，包含
`video_generation` capability，并设置 `video_workflow`。

## 提交任务

```bash
curl -X POST http://127.0.0.1:8000/v1/videos/generations \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "video-6000ada",
    "prompt": "A cinematic landscape with slow camera movement",
    "negative_prompt": "blurry, low quality",
    "size": "832x480",
    "frames": 81,
    "fps": 16,
    "steps": 20,
    "cfg_scale": 5,
    "seed": 42
  }'
```

接口立即返回 `202` 和 `video-...` 任务 ID。使用以下接口轮询：

```bash
curl http://127.0.0.1:8000/v1/videos/generations/<job-id>
```

状态为 `completed` 后，读取响应里的 `content_url` 下载视频。状态可能是 `queued`、`running`、
`completed` 或 `failed`。任务执行期间 Dispatcher 会保持在途计数，阻止其它模型抢占 GPU。

## 图生视频与 MP4 输出

模型注册项设置 `i2v_video_workflow` 后，可在同一个接口传入 `input_image`。该值是 ComfyUI
`input` 目录中的相对文件名，不接受绝对路径或 `..` 路径：

```bash
curl -X POST http://127.0.0.1:8000/v1/videos/generations \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "video-6000ada",
    "input_image": "example.png",
    "prompt": "The subject turns toward the camera, gentle natural motion",
    "size": "512x512",
    "frames": 17,
    "fps": 8
  }'
```

仓库提供的 Wan2.2 5B 图生视频工作流使用 ComfyUI 原生 `CreateVideo` 与 `SaveVideo`，
固定输出 MP4/H.264，便于浏览器和常用播放器预览。

## 工作流模板

仓库提供 Wan2.2 5B 与 Wan2.2 Remix 双模型工作流样例。模板通过精确字符串占位值接收参数，
不依赖 Python 中硬编码节点编号。服务器可以使用自己的未跟踪工作流，但其通用节点能力应先在
开发仓库验证。

视频任务状态当前保存在 Dispatcher 进程内；重启服务会丢失查询记录，但已提交到 ComfyUI 的
任务可能继续执行。生产部署前应据此设置合适的重启窗口。
