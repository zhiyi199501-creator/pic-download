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

## 安装

```bash
pip install -r requirements.txt
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

图片默认保存到 `downloads/<域名>/` 目录。

## 示例

```bash
python download_images.py https://www.python.org
```

输出示例：

```
正在获取页面: https://www.python.org
找到 12 张图片，保存到: /path/to/downloads/www.python.org
[1/12] https://www.python.org/static/img/python-logo.png
  已保存: downloads/www.python.org/001_python-logo.png
...
完成: 成功 12/12
```
