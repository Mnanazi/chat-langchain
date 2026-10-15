# 多模态 Prompt 组装小结

> 结合本项目 `src/middleware/guardrails_middleware.py` 与 `src/middleware/ingress_guards_middleware.py` 的真实代码整理。
> 主题：一条用户消息的 `content` 可能是纯文本，也可能是"文字+图片"的内容块列表，代码如何统一处理并按用途差异化组装。

---

## 一、前提：`content` 的两种形态

LangChain 消息（`HumanMessage` 等）的 `.content` 有两种可能：

- **形态 A：纯字符串** —— 例如 `"你好"`。
- **形态 B：内容块列表** —— 多模态混排，例如：

```python
[
  "帮我看看这张图",                                  # 纯字符串块
  {"type": "text", "text": "..."},                  # 文字块
  {"type": "image_url", "image_url": {"url": "..."}},  # 图片块
]
```

**所有处理函数的第一套路都是**：先 `isinstance(content, str)` 分支处理纯文本；否则按 `list` 遍历每个块，**只对文字块动手，非文字块（图片等）保留原样或替换成占位符**。这是本项目的既定编码规范。

---

## 二、三种"用途"决定三种"处理方式"

同一条多模态内容，在不同用途下的处理策略**并不相同**，核心取舍是：**这个场景需不需要"看图"、要不要泄露图片编码**。

| 用途 | 代表方法 | 对图片块的处理 | 原因 |
|---|---|---|---|
| 日志 / 采样存库 / 预览 | `_content_to_safe_text` | **丢弃**，替换成 `[image attached]` 占位符 | 只需文字；不该把 base64 图片塞进样本/日志 |
| 护栏分类 / 生成婉拒语 | `_build_guardrails_content`、`_build_rejection_content` | **原样保留**图片块 | 需要利用模型的 Vision 能力"图文一起判" |
| 截断超长输入 | `_truncate_content`（ingress_guards） | 保留原样、不占字符额度 | 只截文字长度，图片非字符 |
| 提取纯文本 | `_extract_message_text` | 跳过，仅拼文字块 | 只要文字内容 |

一句话：**存样本/打日志 = 丢图换占位符；做判断/生成回复 = 保留图供视觉模型看。**

---

## 三、逐个方法拆解

### 1. `_content_to_safe_text`（多模态 → 安全纯文本）

把任意 content 压成一段文字，**图片换成占位符、避免编码泄漏**：

- `str` → 原样返回；
- 非 list → `str(content)`；
- list → 遍历：文字块取出文本；`image_url`/`input_image` → `[image attached]`；其它 `type` → `[{type} attached]`；最后空格拼接。

### 2. `_extract_message_text`（只取文字）

从消息提取纯文本，无文字则返回 `None`：`str` 直接 strip；list 仅收集 `type=="text"` 的块文本，空格拼接。用于"有没有可分类的文字 query"的判断。

### 3. `_content_has_media`（是否含非文字块）

`content` 是 list 且存在 `type` 不为 `None`/`text` 的块 → `True`。配合 `_extract_message_text`：即便一条消息**没有文字只有图片**，也应送去分类。

### 4. `_build_guardrails_content` / `_build_rejection_content`（构造发给模型的 prompt）

两者结构一致，都**保留原始图片块**，并区分两条分支：

- **纯文本分支**：直接拼成字符串，形如 `f"{instruction}\n\nUser query: {content}"`。
- **列表分支**：组装成 **三明治结构**（见第四节）。

```
[
  {"text": instruction(+上下文)},   # 前置引导语：指认"以下是用户请求"、提示图文并看
  ...用户原始内容块（文字转 text 块 / dict 块原样）...,
  {"text": "收尾动作指令"},          # 仅 rejection_content：在生成前重申"写一段友好婉拒"
]
```

### 5. `_truncate_content`（入口守卫：截断超长文字）

- `str` → 超 `MAX_MESSAGE_CHARS` 就切片，否则原样返回；
- list → 维护**共享额度** `remaining`，对文字块（`str` 或 `{type:"text"}`）**依次扣减额度截断**，非文字块原样保留；无改动则返回**原对象**（供上层用 `is not` 判断是否截断过）。

---

## 四、重点：多模态 prompt 的"三明治"组装

`_build_rejection_content` 的列表分支产出的序列是：

```
[ 前置引导语 ]  [ 用户文字/图片块… ]  [ 收尾动作指令 ]
```

为什么关键指令要**在开头和结尾各放一次**（位置不同、职责不同，不是冗余）：

1. **前置引导语**（开头）：语义上"向前指"——告诉模型"下面这些才是用户请求，请连同图片一起看"。它给中间那堆原始内容块提供上下文框架。
2. **收尾动作指令**（末尾）：紧贴模型"即将生成"的位置。因为中间的用户内容可能很长、还夹图片，会稀释/淹没开头那条指令；把"该做什么"复述在末尾，能利用大模型对**靠近输出位置内容注意力最强**（recency）的特性，让动作不被遗忘。
3. **纯文本分支不重复**：内容短、无穿插、不涉及多模态时，一次指令（`instruction` 里已含动作）就够——这反证了"结尾复述"是**专为多模态长输入**准备的。

---

## 五、通用套路总结

处理多模态 `content` 的可复用模板：

1. **先分流**：`isinstance(content, str)` 走简单路径；否则按 `list` 遍历。
2. **按块类型区别对待**：只提取/改写 `type=="text"`（或纯 str）的文字块；非文字块（图片等）**保留原样**（供视觉模型）或**替换为占位符**（日志/样本场景）。
3. **按需选择是否携带图片**：判断/生成 = 带图；观测/存储 = 去图。
4. **构造发给模型的 prompt 时用"三明治"**：前置框架语 + 用户内容块 + 末尾动作指令，保证指令在注意力最强的输出位置依然清晰。
5. **需要"是否改动过"判断时，返回原对象本身**（如截断无变化返回原 `content`），让上层用 `is not` 廉价判定。

---

## 附：涉及的本项目代码位置

- `guardrails_middleware.py` `_content_to_safe_text`：多模态转纯文本，图片换占位符。
- `guardrails_middleware.py` `_extract_message_text` / `_content_has_media`：提取文字 / 判断是否含媒体块。
- `guardrails_middleware.py` `_build_guardrails_content` / `_build_rejection_content`：分类与婉拒的 prompt 组装（三明治结构，保留图片）。
- `ingress_guards_middleware.py` `_truncate_content`：多模态列表共享额度截断，保留非文字块。
