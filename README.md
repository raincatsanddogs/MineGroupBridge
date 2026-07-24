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

## 敏感词过滤

Minecraft 发往 QQ 的可见文本可在 `config/mc_qq.yaml` 中选择整条屏蔽或
逐词替换：

```yaml
ignore_word_file: "./src/mc_qq_ignore_word_list.json"
ignore_word_mode: "replace"       # replace 或 block
ignore_word_replacement: "***"    # 没有逐词映射时的默认值
ignore_word_replacements:
  "杀": "哈！"
  "死": "猫"
```

映射键会自动加入敏感词库。替换模式支持全半角、大小写、跨空白以及带声调
的中文同音字匹配；例如“沙”和“杀”同为 `sha1`，会采用“杀”的映射，
“啥（sha2）”则不会命中。多音字按照词组上下文确定读音。

外部 JSON 词库继续使用 `ignore_word_file`，并同时支持普通词和逐词映射：

```json
{
  "words": ["其他敏感词"],
  "replacements": {
    "杀": "哈！",
    "死": "猫"
  }
}
```

同一个词在两处配置时，`mc_qq.yaml` 的映射优先。敏感词只处理 MC→QQ
的文字；成就图片内部文字不会被修改。

## 使用和参考

本项目直接使用了：[17TheWord/nonebot-plugin-mcqq](https://github.com/17TheWord/nonebot-plugin-mcqq)的代码

资源文件为：[Owen1212055/mc-assets](https://github.com/Owen1212055/mc-assets)、[InventivetalentDev/minecraft-assets](https://github.com/InventivetalentDev/minecraft-assets)

minecraft字体来源：[minebbs](https://www.minebbs.com/resources/11063/)
