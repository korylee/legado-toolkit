# Skill：漫画/小说书源生成 v2

## 一、技能用途

给定一个漫画或小说站点 URL，按照标准化流程分析站点结构，生成可直接导入 Legado（阅读）App 的书源 JSON。

## 二、核心工作流

### 第 1 步：手动搜索，确认 URL 参数

在浏览器打开站点，手动搜索一个关键词，观察地址栏：

| 站点 | 搜索 URL | 参数名 |
| :--- | :--- | :--- |
| 漫画号 | `/search?q=测试` | `q` |
| 豆包漫画 | `/search?q=系统` | `q` |
| COLAMANGA | `/search?searchString=系统&page=1` | `searchString` |
| 零搬运 | `/Android/souo/?keyword=我的` | `keyword` |
| 追更漫画 | `/search?key=系统` | `key` |

> 注意：`__searchtoken__` 之类的 token 参数通常可以省略，先试省略版。

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

### 第 5 步：判断章节页图片类型

打开任意章节页，查看源码：

| 源码特征 | 图片位置 | 方案 |
| :--- | :--- | :--- |
| `<img src="https://...">` | 直接可见 | 直接抓 `src` |
| `<img data-original="https://...">` | 懒加载 | 抓 `data-original` |
| `src="blob:https://..."` | JS 临时地址 | 从数据源头取真实地址 |
| 只有占位符 `#mangalist` | JS 动态渲染 | 加 `webView` |
| 有 `C_DATA='...'` | 加密 | 尝试解密或 webView |

### 第 6 步：处理图片防盗链

图片手动能打开但 App 里不显示 → 加 `Referer`：

```js
'<img src="' + url + ',{\"headers\":{\"Referer\":\"https://站点域名/\"}}">'
```

## 三、书源模板

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
    "webView": true,
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

## 四、高频坑速查

| 现象 | 原因 | 解决 |
| :--- | :--- | :--- |
| 搜索无结果 | 参数名写错 | 手动搜一次看 URL |
| 列表为空 | 选择器不对 | F12 确认容器类名 |
| 列表为空但浏览器可见 | AJAX 动态填充 | 找 JSON 接口或加 `webView` |
| 章节页只有占位符 | JS 动态渲染 | `chapterUrl` 加 `webView` |
| `src` 是 `blob:` | 临时地址 | 从 `params` 取真实地址 |
| 图片能开 App 不显示 | 防盗链 | 加 `Referer` 头 |
| 内容为空但 webJs 执行了 | 返回值格式不对 | `webJs` 返回换行分隔地址，`content` 用 `@js` 转 `<img>` |
| JS 执行超时 | 轮询 DOM 或页面脚本阻塞 | 直接读数据源头，或 `legado.sleep` 等渲染 |
| `webJs` 报语法错误 | 用了 ES6+ 语法 | 改写成 ES5（见第六节） |

## 五、特殊场景处理

### 场景 1：`params.chapter_images` 加密

页面源码有 `var params = '加密字符串'`，等页面自身 JS 解密完，`params` 变成 object：

```js
var imgs = params.chapter_images;
var host = params.images_domain || params.cdnurl || '';
if (imgs && imgs.length > 0) {
    result = imgs.map(function(x){
        if (/^https?:/.test(x)) return x;
        return host.replace(/\/+$/, '') + '/' + x.replace(/^\/+/, '');
    }).join('\n');
} else {
    result = '';
}
```

### 场景 2：`C_DATA` 深度混淆加密

如 COLAMANGA，`C_DATA` 是 AES 加密，密钥被混淆在 `manga.read.js` 里。若无法还原密钥：

- 放弃手动解密
- 用 `webView` + `legado.sleep(5000)` 等页面自身解密
- 若仍失败，说明加密未破解，暂时无法做源

### 场景 3：相对路径拼接

图片地址是 `/static/...`，在 JS 里拼域名：

```js
return 'https://站点域名' + x;
```

### 场景 4：`data-original` 懒加载

```json
"coverUrl": "tag.img@data-original"
```

### 场景 5：协议相对地址

图片地址是 `//img.xxx.com/...`，需补 `https:`：

```js
if (x.indexOf('//') === 0) x = 'https:' + x;
```

### 场景 6：多域名 CDN

```js
var host = params.images_domain || (params.images_hosts && params.images_hosts[0]) || params.cdnurl || '';
```

### 场景 7：图片地址 base64 编码

```js
var real = atob(x);
```

### 场景 8：Cloudflare 挑战

书源基本无解。`__CF$cv$params` 脚本会拦截非浏览器请求。只能等或换站。

### 场景 9：需要登录

用 `header` 带 Cookie，或在登录配置里设置。

### 场景 10：搜索页 DOM 为空（AJAX）

1. F12 → Network → 筛选 `XHR` 或 `search`
2. 找到返回 JSON 的请求，复制 URL 和返回结构
3. `searchUrl` 改成接口地址，`ruleSearch` 用 JSONPath（`$.data.list[*]`）

## 六、Legado JS 引擎兼容性（重要）

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
| `Object.keys()` | 兼容，可用 |
| `JSON.parse` / `JSON.stringify` | 兼容，可用 |

**ES6 改写示例：**

```js
// 不要用（ES6）
const imgs = Array.from(document.querySelectorAll('img'));
result = imgs.map(i => i.src).join('\n');

// 改成（ES5）
var imgs = document.querySelectorAll('img');
var urls = [];
for (var i = 0; i < imgs.length; i++) {
    urls.push(imgs[i].src);
}
result = urls.join('\n');
```
## 七、调试方法

### 通用调试步骤

1. 搜索能出结果 → 搜索规则正确
2. 详情页能加载 → 详情规则正确
3. 目录能加载 → 目录规则正确
4. 章节页有图 → 内容规则正确
5. 用 App 调试功能看"正文源码"

### 调试 `webJs` 返回值

```js
var info = [];
info.push('C_DATA=' + (typeof C_DATA !== 'undefined'));
info.push('__cr=' + (typeof __cr !== 'undefined'));
var imgs = document.querySelectorAll('图片容器 img');
info.push('img数=' + imgs.length);
for (var i = 0; i < Math.min(imgs.length, 3); i++) {
    info.push('img[' + i + '].src=' + (imgs[i].src || '').substring(0, 80));
}
result = info.join('\n');
```

### 调试 `params` 字段名

```js
result = JSON.stringify(Object.keys(params));
```

### 常用 Legado API

| API | 用途 |
| :--- | :--- |
| `java.ajax(url)` | 同步请求，返回 HTML |
| `java.ajax(url, header)` | 带请求头请求 |
| `java.get(url)` / `java.post(url, body)` | 更细粒度的请求 |
| `legado.log(msg)` | 输出日志 |
| `legado.toast(msg)` | 弹提示 |
| `legado.sleep(ms)` | 阻塞等待（慎用） |
| `legado.browser.run(url, js, opt)` | 独立浏览器环境执行 JS |
| `baseUrl` | 当前页面 URL |
| `result` | `webJs` 的返回值 |

### `legado.browser.run` 参数

```js
legado.browser.run(url, jsCode, {
    waitUntil: 'load',  // 'load' | 'domcontentloaded' | 'networkidle'
    timeout: 30000      // 超时毫秒
})
```

> `networkidle` 在广告/统计脚本多的站点会永远等不到，建议用 `load`。

## 八、图片处理进阶

### 多属性懒加载

优先级：`data-original` → `data-src` → `data-lazy-src` → `src`

```js
var u = img.getAttribute('data-original') || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.src || '';
```

### `srcset` 处理

```js
var srcset = img.getAttribute('srcset');
if (srcset) {
    var u = srcset.split(',')[0].trim().split(' ')[0];
}
```

### 模拟滚动触发懒加载

```js
window.scrollTo(0, document.body.scrollHeight);
```

## 九、书源元数据与配置

```json
{
  "bookSourceName": "站点名",
  "bookSourceGroup": "漫画",
  "bookSourceComment": "备注，如最后更新日期",
  "bookSourceUrl": "https://站点域名",
  "bookSourceType": 2,
  "customOrder": 0,
  "enabled": true,
  "enabledExplore": true,
  "header": "{\"User-Agent\":\"Mozilla/5.0 (Linux; Android 10)\"}",
  "loginUrl": "",
  "charset": "utf-8"
}
```

| 字段 | 说明 |
| :--- | :--- |
| `bookSourceType` | 0=文本，1=音频，2=漫画，3=文件 |
| `bookSourceGroup` | 分组名，多个用逗号或 `&&` |
| `enabledExplore` | 是否启用发现 |
| `header` | 全局请求头，JSON 字符串 |
| `charset` | GBK 页面需指定，如 `gbk` |

## 十、检查清单

| 步骤 | 检查内容 |
| :--- | :--- |
| 1. 搜索 | URL 参数名是 `q` / `keyword` / `key` / `searchString`？ |
| 2. 搜索列表 | 容器类名是否正确？封面是 `src` 还是 `data-original`？ |
| 3. 搜索列表为空 | DOM 是否为空？为空则找 AJAX 接口 |
| 4. 详情页 | 书名、作者、封面、简介选择器是否正确？ |
| 5. 目录页 | 章节列表容器和链接选择器是否正确？ |
| 6. 章节页 | 源码里有没有 `<img>`？没有就是动态渲染 |
| 7. 动态渲染 | `chapterUrl` 加 `webView:true` |
| 8. 找数据源 | 搜索 `params`、`chapter_images`、`image_list` |
| 9. 加密数据 | 等页面自己解密，直接读 object |
| 10. blob 地址 | 放弃 DOM，从数据源头取真实地址 |
| 11. 防盗链 | 图片手动能开但 App 不显示，加 `Referer` |
| 12. JS 兼容 | 是否用了 ES6 语法？改写成 ES5 |
| 13. 分页 | 搜索/目录/正文是否需要 `lastPage` / `nextTocUrl` / `nextContentUrl` |
| 14. 发现 | 是否需要 `exploreUrl` + `ruleExplore` |
| 15. 请求头 | 是否需要 `header` 带 UA / Referer / Cookie |
| 16. 调试 | 用 App 的"调试"功能看"正文源码" |

## 十一、实战案例索引

| 站点 | 类型 | 关键点 |
| :--- | :--- | :--- |
| 漫画号 | 动态 | `chapterUrl` 加 `webView`，`params.chapter_images` |
| 豆包漫画 | 动态 | `coverUrl` 用 `data-original`，章节列表 `ewave-playlist-sort-content` |
| 零搬运 | 静态 | 直接抓 `src`，加 `Referer` |
| COLAMANGA | 加密 | `C_DATA` AES 加密，密钥混淆，未破解 |
| 追更漫画 | AJAX | 搜索列表 DOM 为空，需找 JSON 接口 |

## 十二、风险与合规

- 书源仅供个人学习，不要用于商业用途
- 尊重站点版权，不要高频请求
- 不要公开传播付费内容
- 站点改版、加密升级、域名更换都会导致源失效，需定期检查

## 十三、一句话总结

**先看源码有没有 `<img>`，没有就 `webView`；有 `params` 就等它解密，直接读对象；`blob:` 别碰，找真实地址；图片能开但不显示，加 `Referer`；搜索列表为空，找 AJAX 接口；Legado 只支持 ES5，别写箭头函数。**
