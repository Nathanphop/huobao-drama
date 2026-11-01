import { i18n, setUiLocale, UI_LOCALES, type UiLocale } from '~/composables/i18n'
import { settingsAPI } from '~/composables/useApi'

export default defineNuxtPlugin(async (nuxtApp) => {
  nuxtApp.vueApp.use(i18n)
  // Backend content language is the shared source of truth across browser
  // surfaces. Hydrate UI locale before rendering to prevent UI/AI drift.
  try {
    const language = (await settingsAPI.contentLanguage())?.language
    if (UI_LOCALES.some(locale => locale.value === language)) setUiLocale(language as UiLocale)
  } catch { /* backend unavailable: retain the browser's stored locale */ }
})
