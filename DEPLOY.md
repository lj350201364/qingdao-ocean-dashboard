# 青岛海况大屏免费部署说明

## 推荐方案：Koyeb

Koyeb 免费层适合这个项目，因为项目是一个轻量 Python Web 服务。

步骤：

1. 把本文件夹上传到 GitHub 仓库。
2. 打开 Koyeb，选择 `Create Web Service`。
3. 选择 GitHub 仓库。
4. 设置启动命令：

```bash
python OceanWindow_optimized.py --server
```

5. 设置环境变量：

```bash
SERVER_MODE=1
```

6. 部署完成后，Koyeb 会生成一个 HTTPS 访问地址。

## 备选方案：Render

Render 也可以部署这个项目，仓库中已经包含 `render.yaml` 和 `Procfile`。

步骤：

1. 把本文件夹上传到 GitHub 仓库。
2. 打开 Render，创建 `Web Service`。
3. 连接 GitHub 仓库。
4. Build Command：

```bash
pip install -r requirements.txt
```

5. Start Command：

```bash
python OceanWindow_optimized.py --server
```

6. 部署完成后，Render 会生成一个 HTTPS 访问地址。

## 注意

- 云端部署不会打开桌面窗口，只提供网页访问。
- 本地运行桌面窗口仍然可以直接运行：

```bash
python OceanWindow_optimized.py
```

- 云端免费服务可能会休眠，首次打开会稍慢。
