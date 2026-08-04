# MineGroupBridge

使用 NoneBot2，实现 Minecraft 与 QQ 群的桥接。
目前有将 Minecraft 成就渲染为图片的功能。

## 部署与使用

1.安装依赖：

```bash
pip install -r requirements.txt
```

2.将 `exampleconfig/` 重命名为 `config/`，并修改其中的配置。

3.启动项目

```bash
nb run
```

## 群组at功能

在minecraft发送` @[群组成员id] `，可以at群组成员，@和[群组成员id]之间可以有其他字符，@前需有空格。

## Minecraft → QQ 富媒体

开启以下配置后，Minecraft 玩家聊天中的 `CICode` 和 `ChatUpgrade`
标记会转换成 QQ 图片、音频或视频消息：

```yaml
mc_to_qq_rich_media_enable: true
mc_to_qq_max_media_per_message: 4
chat_upgrade_media_url_template: null
```

支持的格式如下。Tag 名和参数名不区分大小写，但不支持 `ChatUpdate`
等其他拼写；参数值不支持逗号或引号转义。

```text
[[CICode,url=https://example.com/a.png,name=图片名称]]
[[ChatUpgrade,url=https://example.com/a.png,name=图片名称,type=image]]
[[ChatUpgrade,url=https://example.com/a.mp3,name=音频名称,type=audio]]
[[ChatUpgrade,url=https://example.com/a.mp4,name=视频名称,type=video]]
```

`CICode` 始终按图片处理。`ChatUpgrade` 未填写 `type` 时，普通 HTTP(S)
URL 默认按图片处理。无法解析、平台不支持或发送失败的媒体会恢复为原始
bracket 文本；单条消息超过 `mc_to_qq_max_media_per_message` 的部分同样保留
原文。

对于 `chat-upgrade://media/<type>/<mediaId>` 私有引用，可配置 HTTP(S)
转换模板：

```yaml
chat_upgrade_media_url_template: "https://media.example/{type}/{media_id}"
server_dict:
  my_minecraft_server:
    # 服务器级配置优先于全局模板
    chat_upgrade_media_url_template: "https://mc.example/media/{media_id}"
```

模板必须包含 `{media_id}`，可选 `{type}`。桥接器不会下载远程媒体，而是把
转换后的 URL 交给 QQ/OneBot 后端获取。OneBot 可在一条消息中承载多个媒体；
QQ 官方群会按媒体拆分为多次发送；QQ 频道只原生发送图片，音频和视频保留
bracket 文本。拆分后的每次 API 调用分别计入机器人 RPM/RPH。

该开关只影响 Minecraft→QQ；已有的 `chat_image_enable` 仍仅控制 QQ 图片转成
Minecraft `CICode` 的反向链路。

## 敏感词过滤

Minecraft 发往 QQ 的可见文本可在 `config/mc_qq.yaml` 中设置全局处理模式，
并按词覆盖为整条屏蔽或逐词替换：

```yaml
ignore_word_file: "./src/mc_qq_ignore_word_list.json"
ignore_word_mode: "replace"       # replace 或 block
ignore_word_replacement: "***"    # 没有逐词映射时的默认值
ignore_word_replacements:
  "杀": "a"
  "死": "b"
ignore_word_rules:
  "a":
    mode: "replace"
    replacement: "c"
  "b":
    mode: "block"
```

规则键和映射键都会自动加入敏感词库。没有逐词规则时继续使用
`ignore_word_mode`；逐词 `replace` 没有填写 `replacement` 时，依次使用
`ignore_word_replacements` 中的同词映射和 `ignore_word_replacement`。
替换模式支持全半角、大小写、跨空白以及带声调的中文同音字匹配；例如“沙”
和“杀”同为 `sha1`，会采用“杀”的映射，“啥（sha2）”则不会命中。
多音字按照词组上下文确定读音。

外部 JSON 词库继续使用 `ignore_word_file`，并同时支持普通词、逐词映射和规则：

```json
{
  "words": ["其他敏感词"],
  "replacements": {
    "杀": "哈！",
    "死": "猫"
  },
  "rules": {
    "a": {
      "mode": "replace",
      "replacement": "c"
    },
    "b": {
      "mode": "block"
    }
  }
}
```

同一个词在两处配置时，`mc_qq.yaml` 中的同类逐词配置优先。若一条消息
最终选中的命中包含 `block` 规则，整条消息都不会发出。敏感词只处理
MC→QQ 的文字；成就图片内部文字不会被修改。

## 配置热重载

运行时会使用系统文件事件监听启动时选定的 `mc_qq.yaml`、
`render.yaml`（通常位于 `config/`），以及 `ignore_word_file` 当前指向的
敏感词 JSON。监听器不进行定时轮询；相同内容的重复文件事件也不会触发
配置更新，运行中也不会在根目录配置与 `config/` 配置之间自动切换。

除 `command_header`、`command_priority`、`command_block` 外，YAML
配置在保存后自动生效。上述命令 Matcher 配置以及 `.env`、`.env.*`
仍需重启进程。通过 NoneBot/env 提供的 `mc_qq` 顶层字段会在启动时固定，
并持续覆盖 YAML 中的同名顶层字段。

`advancements.json`、资源包 ZIP 等渲染数据文件不在监听范围内，修改后同样
需要重启。

YAML 缺失、格式错误或校验失败时会应用默认配置。敏感词 JSON 热重载失败时
保留该路径上一份有效词库和过滤器；首次启动或切换到尚无有效快照的新路径时，
仅使用 YAML 词条。删除 JSON 文件会清空外部词表，但不影响 YAML 词条。
限流队列中已经完成过滤的消息不会因词表热载而重新处理，新词表只作用于之后
进入队列的消息。

## 使用和参考

本项目直接使用了：[17TheWord/nonebot-plugin-mcqq](https://github.com/17TheWord/nonebot-plugin-mcqq)的代码

资源文件为：[Owen1212055/mc-assets](https://github.com/Owen1212055/mc-assets)、[InventivetalentDev/minecraft-assets](https://github.com/InventivetalentDev/minecraft-assets)

minecraft字体来源：[minebbs](https://www.minebbs.com/resources/11063/)
