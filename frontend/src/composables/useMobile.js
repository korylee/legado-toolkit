import { ref } from "vue";

// 全局单例：整个应用共用同一个 matchMedia，避免每个组件各建一个监听
const QUERY = "(max-width: 900px)";
const isMobile = ref(false);
let mq = null;

if (typeof window !== "undefined" && window.matchMedia) {
  mq = window.matchMedia(QUERY);
  isMobile.value = mq.matches;
  const onChange = (e) => { isMobile.value = e.matches; };
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else if (mq.addListener) mq.addListener(onChange);   // 老 Safari
}

export function useMobile() {
  return isMobile;
}
