# 票序

票序是一款自部署的发票管理工具，支持连接多个 QQ 邮箱、自动收取发票，也支持批量手动导入。

- 支持 PDF、OFD、XML 和 JPG/PNG 发票，提供本地识别及可选的 AI 辅助识别。
- 支持发票去重、人工核对、按导入时间整理，以及创建报销组、标记报销状态。
- 支持邮件查看与重新识别、原件 ZIP 导出、自定义文件名，以及完整备份和恢复。
- 支持 Docker 部署，适用于 AMD64 和 ARM64 设备。

Docker Hub 镜像为 `mosetian/piaoxu:latest`，同一标签同时支持 AMD64 和 ARM64，Docker 会自动选择适合设备的架构。

下载仓库后，使用 `compose.hub.yaml` 部署（默认 PUID/PGID 为 `1000:10`，按设备实际用户修改）：

```sh
docker compose -f compose.hub.yaml pull
docker compose -f compose.hub.yaml up -d
```

访问 <http://设备IP:8765/>。首次登录账号和密码均为 `admin`，请登录后在“偏好设置”中及时修改。配置与数据库保存在 `config/`，发票原件保存在 `data/`。更新或重建容器时继续映射原目录即可保留全部数据，请妥善备份，不要公开分享。

本地源码开发可继续使用 `docker compose up -d --build`，访问 <http://127.0.0.1:8765/>。

GitHub Actions 会在提交到 `main` 后先运行测试，再推送多架构 `latest` 和 `sha-提交号` 镜像；推送 `v*` Git 标签时生成同名镜像标签。也可从 Actions 页面手动运行。工作流使用仓库密钥 `DOCKER_HUB_USERNAME` 和 `DOCKER_HUB_TOKEN`，密钥值无需写入代码。

发票识别结果需要人工核对，本项目不提供发票真伪查验。
