# 7gogo Talk 存档工具

用于保存公开的 7gogo talk 页面、接口数据和媒体资源，并通过本地 HTTP 服务访问。请只在合法、获授权且符合网站规则的情况下使用，并遵守访问频率限制。

> **Vibe Coding 声明：** 本项目大量使用了 AI 辅助开发和维护。正式使用前请自行审查代码、在实际环境中测试，并确认其行为符合你的需求。

## 快速开始

安装唯一的外部依赖：

```bash
python3 -m pip install -r requirements.txt
```

执行一次完整抓取。`--talk-id` 可以填写 7gogo 完整网址，也可以填写 talk slug：

```bash
python3 archive_7gogo.py \
  --talk-id https://7gogo.jp/nishino-nanase \
  --phase all \
  --workers 8
```

例如抓取另一个 talk：

```bash
python3 archive_7gogo.py --talk-id saito-asuka --phase all --workers 8
```

默认 talk 是 `nishino-nanase`，因此可以省略 `--talk-id`。已有文件默认跳过，重复执行同一命令即可继续未完成的任务；使用 `--overwrite` 才会重新请求已有数据。

`all` 会完成网页和静态资源、消息、消息详情、评论、点赞、转发、用户资料、talk 资料、公共接口数据、图片和视频的抓取，这是普通使用方式。单独阶段主要用于重试或补抓某一部分。

## 常用参数

```text
--workers N                 并发接口和媒体下载数，默认 4
--timeout SECONDS           HTTP 请求超时秒数，默认 300
--delay SECONDS             顺序请求之间的间隔，默认 0.5
--post-id ID[,ID...]        只处理指定消息
--follower-max-pages N      每次抓取的 follower 页数，默认 10；0 表示全部
--overwrite                 覆盖已有 JSON 和媒体文件
--refresh-base              在 --phase all 时重新抓取网页、资源和消息
--output DIRECTORY          指定 talk 输出目录
```

可用阶段为 `base`、`details`、`comments`、`good`、`retalks`、`users`、`support`、`images`、`videos` 和 `all`。

follower 接口每页只有 10 条，可能有数十万条记录。脚本默认每次抓取 10 页，并保存游标；再次执行时会从下一页继续。只有确实需要完整 follower 存档时才建议使用 `--follower-max-pages 0`。

`images` 阶段会下载引用到的 `stat.7gogo.jp` 图片，并将 JSON、HTML、CSS、JavaScript 中的地址改写为 `/talk-id/...` 本地地址。`videos` 阶段还会下载直链视频、HLS 播放列表、HLS 分片和视频缩略图。

## 启动本地服务

在项目目录运行：

```bash
python3 server.py --host 0.0.0.0 --port 8080
```

访问 talk 时保留末尾的 `/`：

```text
http://127.0.0.1:8080/nishino-nanase/
```

服务器默认使用 `archives/` 作为 Web 根目录，提供保存的本地 API 响应，会自动将不带末尾 `/` 的 talk 地址重定向到规范地址；用户页面仍使用 `/users/{userId}` 路径，并且已禁用原网站 WebSocket 连接。

## 存档目录

```text
.
├── archive_7gogo.py
├── server.py
├── requirements.txt
├── README.md
├── README.zh-CN.md
└── archives/
    ├── talks.json              # talkId、userId 和 slug 对应关系
    ├── data/                   # 公共接口数据和默认头像
    │   └── images/default_user.jpg
    └── nishino-nanase/
        ├── index.html
        ├── original/           # 原始网页和静态资源备份
        ├── appimg_images/      # 本地 stat.7gogo.jp 图片
        ├── output/              # 本地视频和 HLS 资源
        └── data/
            ├── posts/
            ├── comments/
            ├── good/
            ├── retalks/
            ├── profile/
            └── user/
```

生成的网页和保存的数据使用稳定的 talk 根路径，例如 `/nishino-nanase/appimg_images/...` 和 `/saito-asuka/output/...`，因此在多个 talk 之间切换时不会依赖当前 SPA 路由。
