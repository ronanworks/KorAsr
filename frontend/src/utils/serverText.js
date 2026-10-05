// Fixed strings the server writes into data fields (contract docs/dev/). They are compared with
// what arrives and never shown as interface text, so they are spelled with escapes here and
// stay out of the locale files:
//   ZH_TRANSLATING  '翻译中...'     placeholder in the legacy `zh` field while translating
//   ZH_RECOGNIZING  '识别中...'     placeholder in the legacy `zh` field of an interim card
//   ZH_UNAVAILABLE  '翻译暂不可用'  legacy `zh` value of a sentence whose translation failed
//   NO_CONTENT      '无实质内容'    the per-minute summary of a window without substance,
//                                   （无实质内容） (backend/polish/translator.py NO_CONTENT)
export const ZH_TRANSLATING = '\u7ffb\u8bd1\u4e2d...'
export const ZH_RECOGNIZING = '\u8bc6\u522b\u4e2d...'
export const ZH_UNAVAILABLE = '\u7ffb\u8bd1\u6682\u4e0d\u53ef\u7528'
export const NO_CONTENT = '\u65e0\u5b9e\u8d28\u5185\u5bb9'
