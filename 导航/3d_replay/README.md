# 2.5D 立体导航原型

这是比赛 MVP 的前端回放层：不使用摄像头定位，虚拟相机沿 `route` 路线前进；手机用长按按钮推进、设备左右转动看向，电脑用 `W` 前进并拖动鼠标改变视角。

## 运行

由于浏览器 ES Module 和手机陀螺仪需要安全上下文，请在此目录启动静态服务器：

```bash
cd /Users/xujiawei/Projects/导航/3d_replay
python3 -m http.server 4173
```

然后打开 `http://localhost:4173`。手机测试时使用同一局域网 IP；iOS 陀螺仪授权通常要求 HTTPS，建议后续用 Cloudflare Tunnel 或部署到静态站点。

## 与 iPhone 视频输入的衔接

当前 `app.js` 使用手工路线数据。下一步把 `route` 替换为后端生成的 `route.json`：

1. FFmpeg 从 1080p/30fps 视频按 1 fps 抽取关键帧；0.5x 只影响取景范围，不影响路线回放。
2. OCR 提取招牌、楼层和方向文字。
3. 单目深度模型估计相对深度；视觉里程计估计相机轨迹。
4. 人工在路线编辑器确认距离、拐点和路标位置。
5. 导出 `route.json`、路标缩略图和 GLB 资源。

建议第一版限制视频为 1～3 分钟、处理分辨率 720p，避免 M1 8GB 内存压力。
