# LibreOffice 转换服务

`POST /convert?target_format=pdf`，通过 multipart 字段 `file` 上传文件。
目标格式支持 `pdf`、`docx`、`xlsx`、`pptx`、`png`、`jpg`、`jpeg`，响应为转换后的文件。

## 转换为图片

```sh
curl -f -OJ 'http://localhost:8000/convert?target_format=png' \
  -F 'file=@example.docx'
```

`target_format=jpg` 或 `jpeg` 可输出 JPEG。图片分辨率为 150 DPI。
单页直接返回图片，多页按页码从上到下拼接为一张长图（`image/png` 或 `image/jpeg`）。
保留各页尺寸，左对齐，较窄页面右侧补白；不增加页间间距。
下载文件名通过 `Content-Disposition` 返回，示例中的 `-OJ` 自动使用该文件名。

只返回第二页：

```sh
curl -f -OJ 'http://localhost:8000/convert?target_format=png&page=2' \
  -F 'file=@example.docx'
```

`page` 为可选查询参数，从 1 开始，仅适用于图片输出。不传时拼接所有页；
传入时只渲染该页。非正整数返回 422，超出文档页数或用于非图片格式返回 400。
拼接图最多 8000 万像素，JPEG 边长最多 65500 像素；超过限制返回 400，可指定 `page` 分页获取。

Office 文档先经 LibreOffice 转为 PDF，再逐页渲染；PDF 输入直接渲染。
分页遵循 PDF 的打印布局，电子表格可能因打印区域和缩放设置产生多张图片。
整个流程占用一个并发名额，`LIBREOFFICE_CLI_TIMEOUT` 分别限制 PDF 转换和图片渲染命令。
图片渲染使用 [Poppler pdftoppm](https://manpages.debian.org/bookworm/poppler-utils/pdftoppm.1.en.html)，
Dockerfile 已加入 `poppler-utils`；直接运行 Python 时也需安装该系统依赖。

图片功能从 `0.0.3` 版本提供。发布工作流完成镜像构建后，可通过 `docker compose pull && docker compose up -d` 更新服务。
本地构建并运行：

```sh
docker build -t libreoffice:images --build-arg TARGETARCH="$(docker version --format '{{.Server.Arch}}')" .
docker run -d --name libreoffice-images --init -p 8000:8000 libreoffice:images
```

## Docker Compose 启动

```sh
docker compose up -d
docker compose logs -f libreoffice
```

默认使用 `ghcr.io/42tr/libreoffice:0.0.3`，监听宿主机 8000 端口，并发为 4。
可以通过环境变量或同目录 `.env` 文件覆盖端口和转换配置，例如：

```sh
LIBREOFFICE_PORT=8080 LIBREOFFICE_MAX_CONCURRENCY=2 docker compose up -d
```

调用示例：

```sh
curl -f 'http://localhost:8000/convert?target_format=pdf' \
  -F 'file=@example.docx' -o result.pdf
```

## 并发配置

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `LIBREOFFICE_MAX_CONCURRENCY` | `4` | 每个 API worker 同时运行的 LibreOffice 转换数 |
| `LIBREOFFICE_QUEUE_TIMEOUT` | `60` | 等待转换名额的最大秒数，超时返回 503，可稍后重试 |
| `LIBREOFFICE_CLI_TIMEOUT` | `180` | 单次命令执行的最大秒数，超时结束进程组并返回 504 |
| `LIBREOFFICE_CLI_PROFILE_DIR` | `/tmp/libreoffice/cli-profile` | 临时任务目录的根路径，须可写；每个任务创建独立用户配置和输出目录 |

以上数值须为正整数，在进程启动时读取。例如：

```sh
LIBREOFFICE_MAX_CONCURRENCY=4 ./start.sh
```

接口在线程池中执行阻塞操作，使用信号量控制转换数；超过上限的任务等待空闲名额。
排队超时从进入转换函数开始计时，不包含上传和等待 API 线程池的时间。
每个任务通过 `-env:UserInstallation=file:///...` 使用独立配置，避免不同命令连接到同一个 LibreOffice 实例。
转换配置和中间输出在成功或失败后清理；上传文件和结果在错误时或响应发送完成后清理。

启动脚本默认使用单个 Uvicorn worker。增加 worker 后，总转换上限为
`worker 数 × LIBREOFFICE_MAX_CONCURRENCY`，跨容器同样累加。
默认并发为 4，可根据实际文档压测 CPU、内存和延迟后调整；设为 1 可恢复串行转换。
每个转换仍需启动一次 LibreOffice，并发提高吞吐的程度取决于机器资源和文档复杂度。

LibreOffice 参数说明：[官方命令行文档](https://help.libreoffice.org/latest/en-GB/text/shared/guide/start_parameters.html)。

## 验证

安装 `requirements.txt` 后运行（Python 标准库 unittest，无额外测试依赖）：

```sh
python3 -m unittest discover -s tests -v
```

有 `soffice` 的环境中可同时运行真实 HTTP 并发转换测试：

```sh
RUN_LIBREOFFICE_TESTS=1 python3 -m unittest discover -s tests -v
```

安装 `poppler-utils` 后，增加 `RUN_IMAGE_TESTS=1` 可运行真实图片测试，覆盖 PDF 到
PNG/JPG/JPEG 的单页、纵向拼接、指定页输出、页码校验，以及两页文档经 LibreOffice 转为图片的完整流程。

跨架构模拟等较慢环境中，可加 `LIBREOFFICE_QUEUE_TIMEOUT=180`，避免首批转换超过默认排队时间而让后续请求返回 503。

测试覆盖并发上限、任务隔离、队列超时、子进程超时清理、API 事件循环响应能力、错误状态及临时文件清理。
真实转换测试同时发出 4 个请求，校验 PDF 文件头和 DOCX 中的对应文档内容。
