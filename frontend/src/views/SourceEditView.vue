<script setup>
import { ref, computed, onMounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { getDetail } from "../api/sources";

const route = useRoute();
const router = useRouter();
const isNew = computed(() => route.name === "source-new");
const loading = ref(false);
const testing = ref(false);
const testKey = ref("我的");
const testResult = ref(null);

const form = ref({
  bookSourceName: "", bookSourceUrl: "", bookSourceType: 0,
  bookSourceGroup: "", bookSourceComment: "", enabled: true,
  searchUrl: "", header: "",
  ruleSearch: { bookList: "", name: "", bookUrl: "", coverUrl: "", author: "", intro: "" },
  ruleToc: { chapterList: "", chapterName: "", chapterUrl: "" },
  ruleContent: { content: "" },
});

async function load() {
  const url = route.query.url;
  if (!url) return;
  loading.value = true;
  try {
    const d = await getDetail(url);
    form.value = { ...form.value, ...d.source };
  } catch (e) {
    ElMessage.error(e.message);
  } finally { loading.value = false; }
}

// 规则试跑：后端用 legado_rules 回放，告诉你解析出几条 + 样例
async function testRules() {
  testing.value = true;
  testResult.value = null;
  try {
    const res = await fetch("/api/rules/test", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: form.value, keyword: testKey.value }),
    });
    if (!res.ok) throw new Error(await res.text());
    testResult.value = await res.json();
  } catch (e) {
    ElMessage.warning("/api/rules/test 尚未实现：" + e.message);
  } finally { testing.value = false; }
}

function save() {
  // 保存前必须 sanitize：bookSourceType 是 ""/[]/"2" 会让 Legado 导入报 IllegalStateException
  const s = JSON.parse(JSON.stringify(form.value));
  s.bookSourceType = Number(s.bookSourceType);
  if (![0, 1, 2, 3].includes(s.bookSourceType)) s.bookSourceType = 0;
  if (typeof s.bookSourceGroup !== "string") s.bookSourceGroup = String(s.bookSourceGroup || "");
  ElMessage.info("保存接口待实现（需先加 /api/sources/save + sanitize）");
  console.log("将提交:", s);
}

onMounted(load);
</script>

<template>
  <div class="page">
    <el-alert type="warning" :closable="false" show-icon style="margin-bottom: 12px"
              title="规则即时试跑是这个项目最有价值的功能：手动新建源最怕盲写规则，有了它当场就知道对不对。" />
    <el-row :gutter="16">
      <el-col :span="14">
        <el-card shadow="never" :header="isNew ? '新建源' : '编辑源'">
          <el-form label-width="110px" size="small">
            <el-form-item label="名称"><el-input v-model="form.bookSourceName" /></el-form-item>
            <el-form-item label="域名">
              <el-input v-model="form.bookSourceUrl" placeholder="https://example.com" />
            </el-form-item>
            <el-form-item label="类型">
              <el-select v-model="form.bookSourceType" style="width: 160px">
                <el-option :value="0" label="📖小说" /><el-option :value="1" label="🎧听书" />
                <el-option :value="2" label="🎨漫画" /><el-option :value="3" label="🎬视频" />
              </el-select>
            </el-form-item>
            <el-form-item label="分组"><el-input v-model="form.bookSourceGroup" /></el-form-item>
            <el-form-item label="搜索 URL">
              <el-input v-model="form.searchUrl" placeholder="/search?q={{key}}" />
            </el-form-item>
            <el-divider content-position="left">ruleSearch</el-divider>
            <el-form-item label="bookList"><el-input v-model="form.ruleSearch.bookList" /></el-form-item>
            <el-form-item label="bookUrl"><el-input v-model="form.ruleSearch.bookUrl" /></el-form-item>
            <el-form-item label="name"><el-input v-model="form.ruleSearch.name" /></el-form-item>
            <el-divider content-position="left">ruleToc</el-divider>
            <el-form-item label="chapterList"><el-input v-model="form.ruleToc.chapterList" /></el-form-item>
            <el-form-item label="chapterUrl"><el-input v-model="form.ruleToc.chapterUrl" /></el-form-item>
            <el-divider content-position="left">ruleContent</el-divider>
            <el-form-item label="content"><el-input v-model="form.ruleContent.content" /></el-form-item>
            <el-form-item>
              <el-button type="primary" @click="save">保存</el-button>
              <el-button @click="router.back()">返回</el-button>
            </el-form-item>
          </el-form>
        </el-card>
      </el-col>

      <el-col :span="10">
        <el-card shadow="never" header="规则试跑">
          <div class="toolbar">
            <el-input v-model="testKey" placeholder="搜索关键词" style="width: 160px" />
            <el-button type="success" :loading="testing" @click="testRules">试跑</el-button>
          </div>
          <el-alert type="info" :closable="false" show-icon
                    title="后端用 core.rules.replayer 回放：class./tag. 简写、@链式、##正则##、JSONPath 都支持；@js:/@xpath/|| 会返回「无法回放」而不是静默失败。" />
          <pre v-if="testResult" class="mono" style="margin-top: 10px">{{ JSON.stringify(testResult, null, 2) }}</pre>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>
