# LibreOffice 转换服务

`POST /convert?target_format=pdf`，通过 multipart 字段 `file` 上传文件。
目标格式支持 `pdf`、`docx`、`xlsx`、`pptx`，响应为转换后的文件。

## Docker Compose 启动

```sh
docker compose up -d
docker compose logs -f libreoffice
```

默认使用 `ghcr.io/42tr/libreoffice:0.0.2`，监听宿主机 8000 端口，并发为 4。
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

跨架构模拟等较慢环境中，可加 `LIBREOFFICE_QUEUE_TIMEOUT=180`，避免首批转换超过默认排队时间而让后续请求返回 503。

测试覆盖并发上限、任务隔离、队列超时、子进程超时清理、API 事件循环响应能力、错误状态及临时文件清理。
真实转换测试同时发出 4 个请求，校验 PDF 文件头和 DOCX 中的对应文档内容。
