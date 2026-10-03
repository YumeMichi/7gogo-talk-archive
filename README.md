# 7gogo Talk Archive

Archive public 7gogo talks and serve the result locally. Use it only where permitted, and respect the site's terms and rate limits.

> **Vibe Coding Notice:** This project was developed with substantial AI assistance. Review the code, test it in your environment, and verify that its behavior is appropriate before relying on it.

## Quick Start

Install the only external dependency:

```bash
python3 -m pip install -r requirements.txt
```

Run one complete archive job. The talk argument can be a 7gogo URL or a talk slug:

```bash
python3 archive_7gogo.py \
  --talk-id https://7gogo.jp/nishino-nanase \
  --phase all \
  --workers 8
```

For example:

```bash
python3 archive_7gogo.py --talk-id saito-asuka --phase all --workers 8
```

The default talk is `nishino-nanase`, so `--talk-id` may be omitted. Existing files are skipped; running the same command again resumes incomplete work. Use `--overwrite` to request existing data again.

`all` includes the page and static resources, posts, post details, comments, likes, reposts, user data, profile data, shared API data, images, and video resources. It is the normal mode; individual phases are mainly for retries or targeted updates.

## Useful Options

```text
--workers N                 parallel API and media downloads (default: 4)
--timeout SECONDS           HTTP timeout (default: 300)
--delay SECONDS             delay between sequential requests (default: 0.5)
--post-id ID[,ID...]        process only selected posts
--follower-max-pages N      follower pages per run (default: 10; 0: all)
--overwrite                 replace existing JSON and media files
--refresh-base              refresh the page/resources/posts during --phase all
--output DIRECTORY          use a custom talk output directory
```

Available phases are `base`, `details`, `comments`, `good`, `retalks`, `users`, `support`, `images`, `videos`, and `all`.

Followers are returned by the service 10 at a time and may contain hundreds of thousands of records. The script fetches 10 pages per run by default and stores its cursor so later runs continue from the next page. Use `--follower-max-pages 0` only when a complete follower archive is intended.

The `images` phase downloads referenced `stat.7gogo.jp` images and rewrites JSON, HTML, CSS, and JavaScript references to local `/talk-id/...` URLs. The `videos` phase additionally downloads direct video files, HLS playlists, HLS segments, and video thumbnails.

## Local Server

Start the local archive server from the project directory:

```bash
python3 server.py --host 0.0.0.0 --port 8080
```

Open a talk with a trailing slash:

```text
http://127.0.0.1:8080/nishino-nanase/
```

The server uses `archives/` as its default web root. It serves archived API responses, redirects a talk URL without its trailing slash, keeps user routes at `/users/{userId}`, and disables the original WebSocket connection.

## Archive Layout

```text
.
├── archive_7gogo.py
├── server.py
├── requirements.txt
├── README.md
├── README.zh-CN.md
└── archives/
    ├── talks.json              # talkId/userId/slug mapping
    ├── data/                   # shared API data and default avatar
    │   └── images/default_user.jpg
    └── nishino-nanase/
        ├── index.html
        ├── original/           # untouched page and downloaded static backups
        ├── appimg_images/      # local stat.7gogo.jp images
        ├── output/             # local video and HLS resources
        └── data/
            ├── posts/
            ├── comments/
            ├── good/
            ├── retalks/
            ├── profile/
            └── user/
```

The generated page and saved data use stable talk-root paths such as `/nishino-nanase/appimg_images/...` and `/saito-asuka/output/...`, so navigation between talks does not depend on the current SPA route.
