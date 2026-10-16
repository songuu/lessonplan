const labels: Record<string, string> = {
  primary: "小学",
  junior: "初中",
  senior: "高中",
  chinese: "语文",
  math: "数学",
  english: "英语",
  physics: "物理",
  chemistry: "化学",
  biology: "生物",
  history: "历史",
  geography: "地理",
  politics: "道德与法治/思想政治",
  science: "科学",
  music: "音乐",
  art: "美术",
  physical_education: "体育与健康",
  information_technology: "信息科技",
  labor: "劳动",
  new_lesson: "新授课",
  review_lesson: "复习课",
  practice_lesson: "练习课",
  experiment_lesson: "实验课",
  reading_lesson: "阅读课",
  writing_lesson: "写作课",
  activity_lesson: "活动课",
  assessment_lesson: "讲评课",
};
const grades = [
  "一年级",
  "二年级",
  "三年级",
  "四年级",
  "五年级",
  "六年级",
  "七年级",
  "八年级",
  "九年级",
  "高一",
  "高二",
  "高三",
];
grades.forEach((name, index) => {
  labels[`grade_${index + 1}`] = name;
});
export const displayValue = (value?: string) =>
  value ? labels[value] || value : "";
