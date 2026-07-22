# 网页图片下载器

输入一个网址，自动抓取页面中的图片并保存到本地。

## 功能

- 解析 `<img>` 的 `src`、`srcset` 及常见懒加载属性（`data-src` 等）
- 解析 `<source>` 标签
- 解析 CSS `background-image` 和内联 `<style>` 中的图片
- 解析 Open Graph / Twitter 分享图
- 自动将相对路径转为绝对 URL
- 跳过 `data:` 内联图片
- 按序号命名，避免文件名冲突
- 多线程并发下载（默认 8 线程）
- 超时自动重试（默认 3 次）
- 支持从 JS 页面源码中提取图片链接
- 针对百度图片（image.baidu.com）走专用接口解析
- 对 JS 动态页面（如 aiaha.xyz）自动用 Playwright 渲染后提取图片

## 安装

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

## 使用

```bash
# 基本用法
python download_images.py https://example.com

# 指定保存目录
python download_images.py https://example.com -o ./my-images

# 设置超时（秒）
python download_images.py https://example.com -t 60
```

图片默认保存到 `downloads/网址名-当前时间/` 目录，例如 `https://www.baidu.com/` 会保存到 `downloads/baidu-20260722-124800/`。

## Web 页面

如果你想通过页面输入网址和保存地址：

```bash
pip install -r requirements.txt
python web_app.py
```

然后打开浏览器访问：

`http://127.0.0.1:5000`

页面包含两个输入框：

- 网址
- 保存地址

## 示例

```bash
python download_images.py https://www.python.org
```

输出示例：

```
正在获取页面: https://www.python.org
找到 12 张图片，保存到: /path/to/downloads/python-20260722-124800
[1/12] https://www.python.org/static/img/python-logo.png
  已保存: downloads/python-20260722-124800/001_python-logo.png
...
完成: 成功 12/12
```
