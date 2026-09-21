# Skill：漫画/小说书源生成

## 一、核心工作流

### 第 1 步：手动搜索，确认 URL 参数

在浏览器里手动搜一次，看地址栏的参数名（`q` / `keyword` / `key` / `searchString`…
**每个站不一样，以地址栏为准**）。`__searchtoken__` 之类的 token 参数通常可以省略，
先试省略版。

### 第 2 步：抓搜索页 DOM，确认列表选择器

按 F12 → Elements，找到包裹每部漫画的容器：

- 容器类名 → `bookList`，格式 `class.你的类名`
- 书名 → `name`
- 详情页链接 → `bookUrl`
- 封面 → `coverUrl`（注意 `src` 还是 `data-original`）
- 作者/简介 → `author` / `intro`

> 若 `div.row` 等列表容器为空，说明是 AJAX 动态填充，去 Network 面板找 JSON 接口。

### 第 3 步：抓详情页 DOM，确认信息选择器

进入任意漫画详情页，确认：

- 书名、封面、作者、简介、最新章节的选择器
- 作者若带"作者："前缀，用 `##作者：##` 正则去掉

### 第 4 步：抓目录页 DOM，确认章节列表

- 章节列表容器 → `chapterList`，格式 `class.你的类名@tag.a`
- 章节名 → `chapterName`
- 章节链接 → `chapterUrl`

> 若 `chapterList` 匹配到排序按钮等无关链接，向上找更精确的父容器。

### 第 5 步：判断章节页的图片在哪

先看源码里有没有真地址：`<img src>` 直接抓；`data-original` / `data-src` 抓那个属性；
`blob:` 或容器是空的 → **地址在 JS 对象里**（见 §四 场景 1）；图片能开但 App 不显示 → 加 `Referer`。

### 第 6 步：处理图片防盗链

图片手动能打开但 App 里不显示 → 加 `Referer`：

```js
'<img src="' + url + ',{\"headers\":{\"Referer\":\"https://站点域名/\"}}">'
```

→ 走一遍这六步就能写出一个能用的源：**容器与字段要成对确认**（`bookList` 写宽了，
`bookUrl` 会先命中导航栏第一条）；形状照 §二 的模板套，卡住先查 §三。

## 二、书源模板

### 静态站（图片直接在 DOM 里）

```json
{
  "bookSourceName": "站点名",
  "bookSourceUrl": "https://站点域名",
  "bookSourceType": 2,
  "searchUrl": "/search?q={{key}}",
  "ruleSearch": {
    "bookList": "class.列表容器",
    "name": "tag.h3@tag.a@text",
    "bookUrl": "tag.h3@tag.a@href",
    "coverUrl": "tag.img@data-original",
    "author": "class.作者类@text##作者：##",
    "intro": "class.简介类@text"
  },
  "ruleBookInfo": {
    "name": "tag.h1@text",
    "coverUrl": "class.封面类@tag.img@data-original",
    "author": "class.作者类@text##作者：##",
    "intro": "class.简介类@text",
    "lastChapter": "class.最新章节@tag.a@text"
  },
  "ruleToc": {
    "chapterList": "class.章节容器@tag.a",
    "chapterName": "tag.a@text",
    "chapterUrl": "tag.a@href"
  },
  "ruleContent": {
    "content": "class.图片容器@tag.img@src",
    "imageStyle": "FULL"
  }
}
```

### 动态站（图片 JS 渲染）

```json
{
  "ruleToc": {
    "chapterList": "class.章节容器@tag.a",
    "chapterName": "tag.a@text",
    "chapterUrl": "tag.a@href##$##,{\"webView\":true}"
  },
  "ruleContent": {
    "content": "@js:result.split('\\n').filter(function(x){return x;}).map(function(x){return '<img src=\"'+x+',{\\\"headers\\\":{\\\"Referer\\\":\\\"https://站点域名/\\\"}}\">';}).join('')",
    "webJs": "var imgs = document.querySelectorAll('图片容器 img'); var urls = []; for (var i = 0; i < imgs.length; i++) { var u = imgs[i].src || imgs[i].getAttribute('data-original') || ''; if (u && u.indexOf('http') === 0) urls.push(u); } result = urls.join('\\n');",
    "imageStyle": "FULL"
  }
}
```

### 接口站（AJAX 返回 JSON）

```json
{
  "searchUrl": "/api/search?key={{key}}",
  "ruleSearch": {
    "bookList": "$.data.list[*]",
    "name": "$.name",
    "bookUrl": "$.url",
    "coverUrl": "$.cover",
    "author": "$.author",
    "intro": "$.intro"
  }
}
```

> 接口地址从 F12 → Network 面板找返回 JSON 的请求。
### 带请求头 / POST 的搜索

```json
{
  "header": "{\"User-Agent\":\"Mozilla/5.0 (Linux; Android 10)\",\"Referer\":\"https://站点域名/\"}",
  "searchUrl": "/search,{\"method\":\"POST\",\"body\":\"key={{key}}&page={{page}}\"}"
}
```

### 带分页的搜索与目录

```json
{
  "searchUrl": "/search?q={{key}}&page={{page}}",
  "ruleSearch": {
    "bookList": "class.列表容器",
    "lastPage": "class.分页@tag.a.-1@text##.*?(\\d+)##$1"
  },
  "ruleToc": {
    "chapterList": "class.章节容器@tag.a",
    "nextTocUrl": "class.下一页@tag.a@href"
  },
  "ruleContent": {
    "nextContentUrl": "class.下一页@tag.a@href"
  }
}
```

### 发现 / 分类页

```json
{
  "exploreUrl": "[{\"title\":\"国产\",\"url\":\"/category/list/1?page={{page}}\"},{\"title\":\"日本\",\"url\":\"/category/list/2?page={{page}}\"}]",
  "ruleExplore": {
    "bookList": "class.列表容器@tag.li",
    "name": "tag.a@title",
    "bookUrl": "tag.a@href",
    "coverUrl": "tag.img@data-original"
  }
}
```

→ 模板按「数据在哪」分：DOM / JS 对象 / 接口。套完形状，用 §三 的坑表按症状对一遍。

## 三、高频坑速查

| 现象 | 原因 | 解决 |
| :--- | :--- | :--- |
| 搜索无结果 | 参数名写错 | 手动搜一次看 URL |
| 列表为空 | 选择器不对 | F12 确认容器类名 |
| 列表为空但浏览器可见 | AJAX 动态填充 | 找 JSON 接口或加 `webView` |
| 章节页只有占位符 | JS 动态渲染 | `chapterUrl` 加 `webView` |
| `src` 是 `blob:` | 临时地址 | 从 `params` 取真实地址 |
| 图片能开 App 不显示 | 防盗链 | 加 `Referer` 头 |
| 内容为空但 webJs 执行了 | 返回值格式不对 | `webJs` 返回换行分隔地址，`content` 用 `@js` 转 `<img>` |
| JS 执行超时 | 轮询 DOM 或页面脚本阻塞 | **没有 `sleep` 可等**：直接读数据源头，或用 `java.webView` 自己在 `js` 里控制时机 |
| `webJs` 报语法错误 | 用了 ES6+ 语法 | 改写成 ES5（见 §五） |

→ 按现象索引；调试时「key 的形态决定走哪条链路」是 App 自己的分派规则（`Debug.kt` 的 when，落点见 `core/app_debug.py` 与 lessons §十四）——工具里的「调试目标」只是前缀构造器，改不了它。

## 四、加密与特殊场景

### 场景 1：`params.chapter_images` 加密（最常见的一类）

页面源码里有 `var params = '加密串'`，页面自己的 JS 解密后 `params` 变成 object。
**三件必知的事**：

1. **`params` 是全局变量**，页面自己是 `params = this.decrypt(params)`（无 `var`）——
   所以 `webJs` 里直接读它，**不必自己实现 AES**。先判类型：解密前它是字符串，
   `typeof params === 'object'` 才是解好的；没解好时返回空串即可，App 会自己重试。
2. **`webJs` 要生效，`chapterUrl` 必须挂 `webView`**。`webView` 是 **URL 规则的选项**
   （`ContentRule` 里没有这个字段，写进去会被静默丢掉），写法是给章节地址追加选项：

   ```json
   "chapterUrl": "tag.a@href##$##,{\"webView\":true}"
   ```

   `##$##` 是「在串尾追加」；工具在请求前会自动剥掉这段。别在 `chapterUrl` 里手写整串。
3. **`xhr_mode: true` 时 DOM 里没有图片地址**（XHR 拉图再挂 `blob:`）：不要「渲染后读
   `img.src`」，只能读解密后的 `params.chapter_images`。

`content` 用 `@js:` 把地址行转成 `<img>`，**要滤掉不像地址的行**——webJs 万一没跑起来，
整页 HTML 会被逐行包成 `<img>`，「静默产出垃圾」比返回空更难查：

```json
"ruleContent": {
  "webJs": "result = (params && typeof params === 'object' && params.chapter_images) ? params.chapter_images.join(String.fromCharCode(10)) : '';",
  "content": "@js:var a = String(result).split(String.fromCharCode(10)), o = [];for (var i = 0; i < a.length; i++) {var s = a[i].trim();if (s && !/[\\s<>]/.test(s)) o.push('<img src=\"' + s + '\">');}result = o.join(String.fromCharCode(10));",
  "imageStyle": "FULL"
}
```

> JS 里**尽量不写反斜杠转义**：换行用 `String.fromCharCode(10)`，这样 JSON 只过一层转义，
> 不会在「JSON → JS 字符串 → 正则」中被吃掉。

站点个案（口袋漫画的图片签名会过期、目录选错会重复 30 章）在 lessons §五十八 四 与 §七十五。

### 其余场景速查

| 现象 | 做法 |
| :--- | :--- |
| 地址是相对路径 `/static/...` | 在 `@js:` 里拼域名：`'https://站点域名' + x` |
| `data-original` 懒加载 | 规则写 `tag.img@data-original` |
| 协议相对 `//img.xxx.com/...` | `if (x.indexOf('//') === 0) x = 'https:' + x;` |
| 多域名 CDN | `params.images_domain \|\| (params.images_hosts && params.images_hosts[0]) \|\| params.cdnurl` |
| 地址是 base64 | `atob(x)` 还原 |
| 需要登录 | `header` 带 Cookie，或用源的登录配置 |
| **Cloudflare 挑战** | **书源基本无解**（`__CF$cv$params` 拦非浏览器请求）：只能等或换站 |
| 搜索列表 DOM 为空（AJAX） | F12 → Network 找返回 JSON 的请求 → `searchUrl` 换成接口地址 + JSONPath（同 §二 接口站模板） |
| `C_DATA` 深度混淆（如 COLAMANGA） | 密钥混淆在站点 JS 里，**没破解就别硬做**：先用 `java.webView` 让页面自己解密，拿不到就换站 |

→ 判据只有一条：**数据在哪就读哪**——DOM 里有就读 DOM，在 JS 对象里就 `webJs` 读对象，
在接口里就走 JSONPath；**别自己实现站点的加密**（密钥会轮换、混淆会变）。

## 五、Legado JS 引擎兼容性（重要）

Legado 的 JS 引擎是 **Rhino**，只支持 ES5，不支持以下语法：

| 不支持的语法 | 改写方式 |
| :--- | :--- |
| 箭头函数 `=>` | `function(){}` |
| `let` / `const` | `var` |
| 模板字符串 `` ` `` | 字符串拼接 `+` |
| `Array.from()` | `for` 循环 + `push` |
| `TextDecoder` | 手动 `String.fromCharCode` |
| `fetch()` | `java.ajax()` |
| `async` / `await` | 回调或同步请求 |
| 展开运算符 `...` | `concat` / `apply` |
→ JS 只当 ES5 写：**箭头函数、`let`/`const`、模板串、展开、`async` 一个都别用**
（`Object.keys` / `JSON.parse` / `JSON.stringify` 可用）。

## 六、调试与常用 API

### 看解密后的字段名

`result = JSON.stringify(Object.keys(params));`（在 `webJs` 里跑，看 `params` 到底有哪些键）

### 常用 API

要记的只有这几个：`java.ajax(url[, timeout])`（第二参数是**超时毫秒**，不是请求头）、
`java.get(url, headers[, timeout])` / `java.post(url, body, headers)`（`headers` 是**必填**的
Map）、`java.connect(url, header)`、`java.log` / `java.toast`、`baseUrl`、`result`
（`webJs` 的返回值）。**完整清单以 `help/JsExtensions.kt` 为准**，别在这里维护第二份。

### 独立浏览器环境（`webView`）

**Legado 没有 `legado.browser.run`，也没有 `sleep`**。要拿渲染后的 DOM，用挂在 `java` 上的
两个：`java.webView(html, url, js)`（在 WebView 里跑 `js` 并返回字符串；`html` 与 `url` 至少
给一个）、`java.startBrowserAwait(url, title)`（开系统浏览器、等用户过验证码）。
`webView` 的真身是**URL 规则的选项**——写进 `ruleContent` 或写成 `legado.browser.*`
都会被静默丢掉。

### 图片处理进阶

- **多属性懒加载**按优先级取：`data-original` → `data-src` → `data-lazy-src` → `src`
- **`srcset`**：取第一段 —— `srcset.split(',')[0].trim().split(' ')[0]`
- **要滚动才加载**：`window.scrollTo(0, document.body.scrollHeight)` 之后再取

→ 要用哪个 `java.*` 先查 `help/JsExtensions.kt`；**没有 `sleep`、没有 `browser`** 这两条
最容易写错（写了不报错、只是拿不到东西）。

## 七、书源元数据与配置

| 字段 | 说明 |
| :--- | :--- |
| `bookSourceType` | 0=文本，1=音频，2=漫画，3=文件（枚举在 `core/models.py`） |
| `bookSourceGroup` | 分组名，**多个用逗号（或分号）分隔**——上游就按 `[,;，；]` 切
（`AppPattern.splitGroupRegex`）。`&&` 是 **`exploreUrl` 多项之间**的分隔，不是分组分隔符 |
| `enabledExplore` | 是否启用发现。**不要手写**：有 `exploreUrl` / `ruleExplore` 才算数，
由配置推导（AGENTS #13——手写实测漂了 885 条），缺这个键时导入链路会自己对齐 |
| `header` | 全局请求头，JSON 字符串 |
| `charset` | GBK 页面需指定，如 `gbk` |

→ 元数据里**能推导的别手写**（`enabledExplore` 由配置推导，见 AGENTS #13）；写完的源先用
「本机引擎」调试一次——与设备无关的那部分结论它就能给。
