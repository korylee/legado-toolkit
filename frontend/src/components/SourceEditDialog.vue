<script setup>
// 新建 / 编辑书源：对话框形态。跳独立页面会丢掉列表的筛选和分页状态。
import { ref, computed, watch } from "vue";
import { ElMessage } from "element-plus";
import { api } from "../api/client";
import { getDetail } from "../api/sources";
import { useMobile } from "../composables/useMobile";

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  sourceUrl: { type: String, default: "" },   // 空 = 新建
});
const emit = defineEmits(["update:modelValue", "saved"]);
const isMobile = useMobile();

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit("update:modelValue", v),
});
const isNew = computed(() => !props.sourceUrl);
const loading = ref(false);
const testing = ref(false);
const which = ref("search");
const testKey = ref("我的");
const testResult = ref(null);

function blank() {
  return {
    bookSourceName: "", bookSourceUrl: "", bookSourceType: 0,
    bookSourceGroup: "", bookSourceComment: "", enabled: true,
    searchUrl: "", header: "",
    ruleSearch: { bookList: "", name: "", bookUrl: "", coverUrl: "", author: "", intro: "" },
    ruleToc: { chapterList: "", chapterName: "", chapterUrl: "" },
    ruleContent: { content: "" },
  };
}
const form = ref(blank());

watch(() => [props.modelValue, props.sourceUrl], async ([show, url]) => {
  if (!show) return;
  testResult.value = null;
  if (!url) { form.value = blank(); return; }
  loading.value = true;
  try {
    const d = await getDetail(url);
    form.value = { ...blank(), ...d.source };
  } catch (e) {
    ElMessage.error("加载源失败: " + e.message);
  } finally {
    loading.value = false;
  }
});

async function testRun() {
  testing.value = true;
  testResult.value = null;
  try {
    testResult.value = await api.post("/rules/test", {
      source: form.value, keyword: testKey.value, which: which.value,
    });
  } catch (e) {
    ElMessage.warning("/api/rules/test 尚未实现（后端待补）");
    testResult.value = { error: String(e.message) };
  } finally {
    testing.value = false;
  }
}

function save() {
  // 保存前必须 sanitize：bookSourceType 为 ""/[]/"2" 会让 Legado 导入报 IllegalStateException
  const s = JSON.parse(JSON.stringify(form.value));
  s.bookSourceType = Number(s.bookSourceType);
  if (![0, 1, 2, 3].includes(s.bookSourceType)) s.bookSourceType = 0;
  if (typeof s.bookSourceGroup !== "string") s.bookSourceGroup = String(s.bookSourceGroup ?? "");
  if (!String(s.bookSourceName || "").trim()) return ElMessage.warning("名称不能为空");
  if (!String(s.bookSourceUrl || "").trim()) return ElMessage.warning("域名不能为空");
  ElMessage.info("保存接口待实现（需先加 POST /api/sources/save + sanitize）");
  emit("saved", s);
}
</script>

<template>
  <el-dialog v-model="visible" :title="isNew ? '新建书源' : '编辑书源'"
             width="1080px" top="4vh" destroy-on-close class="edit-dialog">
    <el-row :gutter="16" v-loading="loading">
      <el-col :span="13">
        <el-form :label-width="isMobile ? 'auto' : '96px'"
                 :label-position="isMobile ? 'top' : 'right'" size="small">
          <el-form-item label="名称"><el-input v-model="form.bookSourceName" /></el-form-item>
          <el-form-item label="域名">
            <el-input v-model="form.bookSourceUrl" placeholder="https://example.com" />
          </el-form-item>
          <el-form-item label="类型">
            <el-radio-group v-model="form.bookSourceType">
              <el-radio-button :value="0">📖小说</el-radio-button>
              <el-radio-button :value="1">🎧听书</el-radio-button>
              <el-radio-button :value="2">🎨漫画</el-radio-button>
              <el-radio-button :value="3">🎬视频</el-radio-button>
            </el-radio-group>
          </el-form-item>
          <el-form-item label="分组">
            <el-input v-model="form.bookSourceGroup" placeholder="📖小说,可用" />
          </el-form-item>
          <el-form-item label="搜索 URL">
            <el-input v-model="form.searchUrl" placeholder="/search?q={{key}}" />
          </el-form-item>

          <el-divider content-position="left">ruleSearch</el-divider>
          <el-form-item label="bookList">
            <el-input v-model="form.ruleSearch.bookList" placeholder="class.book-list@tag.li" />
          </el-form-item>
          <el-form-item label="bookUrl">
            <el-input v-model="form.ruleSearch.bookUrl" placeholder="tag.a@href" />
          </el-form-item>
          <el-form-item label="coverUrl">
            <el-input v-model="form.ruleSearch.coverUrl" placeholder="tag.img@data-original" />
          </el-form-item>

          <el-divider content-position="left">ruleToc</el-divider>
          <el-form-item label="chapterList">
            <el-input v-model="form.ruleToc.chapterList" placeholder="class.chapter@tag.a" />
          </el-form-item>
          <el-form-item label="chapterUrl">
            <el-input v-model="form.ruleToc.chapterUrl" placeholder="tag.a@href" />
          </el-form-item>

          <el-divider content-position="left">ruleContent</el-divider>
          <el-form-item label="content">
            <el-input v-model="form.ruleContent.content" placeholder="id.content@text" />
          </el-form-item>
        </el-form>
      </el-col>

      <el-col :span="11">
        <el-card shadow="never" header="规则试跑">
          <div class="toolbar">
            <el-radio-group v-model="which" size="small">
              <el-radio-button value="search">搜索</el-radio-button>
              <el-radio-button value="toc">目录</el-radio-button>
              <el-radio-button value="content">正文</el-radio-button>
            </el-radio-group>
            <el-input v-model="testKey" size="small" placeholder="关键词" style="width: 120px" />
            <el-button type="success" size="small" :loading="testing" @click="testRun">试跑</el-button>
          </div>
          <el-alert type="info" :closable="false" show-icon style="margin-top: 10px"
                    title="后端用 core.rules.replayer 回放：class./tag. 简写、@链式、.0/.-1、##正则##、JSONPath 都支持；@js:/@xpath:/|| 会回报「无法回放」而不是静默失败。" />

          <div v-if="!testResult" class="muted" style="padding: 22px; text-align: center">
            改完规则点「试跑」，当场就知道能不能解析出结果
          </div>
          <template v-else-if="!testResult.error">
            <p style="margin: 12px 0 6px">
              解析出 <b>{{ testResult.count ?? 0 }}</b> 条
              <el-tag v-if="testResult.unsupported" type="warning" size="small" style="margin-left: 8px">
                无法回放：{{ testResult.unsupported }}
              </el-tag>
            </p>
            <pre class="mono">{{ JSON.stringify(testResult.samples || [], null, 1) }}</pre>
          </template>
          <pre v-else class="mono" style="margin-top: 10px">{{ JSON.stringify(testResult, null, 1) }}</pre>
        </el-card>

        <el-card shadow="never" header="语法速查" style="margin-top: 12px">
          <ul class="muted" style="margin: 0; padding-left: 18px; line-height: 1.9">
            <li>简写：class.xxx → .xxx；tag.a → a</li>
            <li>链式：class.list@tag.li@tag.a@href</li>
            <li>索引：.0 第一个、.-1 最后一个</li>
            <li>正则：规则##正则##替换（支持 $1）</li>
            <li>接口源：$.data.list[*].name</li>
            <li>取图：tag.img@src / @data-original</li>
          </ul>
        </el-card>
      </el-col>
    </el-row>

    <template #footer>
      <el-button @click="visible = false">取消</el-button>
      <el-button type="primary" @click="save">保存</el-button>
    </template>
  </el-dialog>
</template>

<style scoped>
.edit-dialog :deep(.el-dialog__body) { max-height: 76vh; overflow-y: auto; }
pre { background: #f5f7fa; padding: 8px; border-radius: 4px; }
</style>
