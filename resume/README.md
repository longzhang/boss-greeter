# resume/

把你自己的简历放在这个目录里（Markdown 或纯文本都行），然后在
`config.yaml` 的 `greeting.resume_path` 指向它，例如：

```yaml
greeting:
  resume_path: "./resume/我的简历.md"
```

**本目录已整体 gitignore**，只有这份说明会进仓库——简历是私人材料，
里面通常有手机号和邮箱，不该提交。

简历会作为固定前缀走 prompt caching，批量跑时省下大部分输入成本。
内容越具体（技术栈、量级、项目经历），模型越能写出有针对性的开场白。

注意：招呼语会做联系方式校验，简历里的手机号/邮箱若被模型抄进招呼语会被拦下
——BOSS 会屏蔽含联系方式的首条消息。
