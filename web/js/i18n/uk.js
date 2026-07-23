/**
 * Ukrainian (uk) translation strings.
 * Keep keys in sync with en.js — see the note there.
 */

export const uk = {
  "app.wordmark": "TeleVault",

  "nav.chats": "Чати",
  "nav.messages": "Повідомлення",
  "nav.deleted": "Видалені",
  "nav.stats": "Статистика",
  "nav.health": "Стан системи",

  "common.comingSoon": "Цей розділ ще не готовий.",
  "common.loading": "Завантаження…",
  "common.error": "Щось пішло не так.",
  "common.pageOf": "Сторінка {page} з {pages}",
  "common.pageOfPrefix": "Сторінка",
  "common.pageOfSuffix": "з",
  "common.jumpToPage": "Перейти до сторінки",
  "common.first": "Перша",
  "common.last": "Остання",
  "common.prev": "Назад",
  "common.next": "Далі",
  "common.newestFirst": "Спочатку новіші",
  "common.oldestFirst": "Спочатку старіші",
  "common.type.private": "Приватний",
  "common.type.group": "Група",
  "common.type.supergroup": "Супергрупа",
  "common.type.channel": "Канал",

  "chats.empty": "Ще немає архівованих чатів.",
  "chats.noPreview": "Повідомлень ще немає",
  "chats.messagesLabel": "повідомлень",
  "chats.deletedLabel": "видалено",
  "chats.mostRecentFirst": "Нещодавно активні",
  "chats.leastRecentFirst": "Давно активні",

  "messages.empty": "Повідомлень не знайдено.",
  "messages.noText": "(без тексту)",
  "messages.editedLabel": "змінено",
  "messages.searchPlaceholder": "Пошук у тексті повідомлень…",
  "messages.onlyEditedLabel": "Лише змінені",

  "deleted.empty": "Видалених повідомлень не знайдено.",
  "deleted.searchPlaceholder": "Пошук у тексті видалених повідомлень…",
  "deleted.viewDetails": "Показати деталі",
  "deleted.hideDetails": "Сховати деталі",
  "deleted.noRecord": "Запис про видалення не знайдено.",
  "deleted.actor.channel_admin": "Видалено адміністратором каналу",
  "deleted.actor.self": "Видалили ви",
  "deleted.actor.unknown": "Хто видалив — невідомо",
  "deleted.confidence.channel_admin":
    "Лише адміністратор каналу може видалити допис у каналі — звичайні підписники не можуть видаляти дописи.",
  "deleted.confidence.self":
    "Збережені повідомлення доступні лише вам — ніхто інший їх не бачить, а тим паче не може щось із них видалити.",

  "stats.totalMessages": "Усього повідомлень",
  "stats.totalDeleted": "Видалено",
  "stats.totalEdited": "Змінено",
  "stats.totalChats": "Чатів",
  "stats.totalSenders": "Відправників",
  "stats.archivingSince": "Архівується з",
  "stats.perChatTitle": "Розбивка по чатах",
  "stats.empty": "Даних про чати ще немає.",
  "stats.tableChat": "Чат",
  "stats.tableMessages": "Повідомлень",
  "stats.tableDeleted": "Видалено",
  "stats.tableEdited": "Змінено",
  "stats.tableLastSeen": "Останнє повідомлення",

  "health.statusOk": "Усе добре",
  "health.statusDegraded": "Є проблеми",
  "health.dbReadable": "База даних доступна",
  "health.sessionExists": "Сесію Telegram знайдено",
  "health.messageCount": "Заархівовано повідомлень",
  "health.refresh": "Оновити",

  "theme.toggleLabel": "Змінити тему",
  "lang.selectLabel": "Мова",

  // Backfill additions
  "nav.backfill": "Імпорт історії",

  "backfill.aboutTitle": "Про імпорт історії",
  "backfill.disclaimerSession":
    "Для імпорту історії потрібне окреме з'єднання з Telegram. Спершу зупиніть архівацію (main.py) — Telegram дозволяє лише одну активну сесію одночасно.",
  "backfill.disclaimerDeleted":
    "Повідомлення, видалені ще до того, як чат почав архівуватися, відновити неможливо — API історії Telegram повертає лише ті дані, які існують на даний момент.",
  "backfill.disclaimerEdits":
    "Завантажені повідомлення зберігаються лише в їхньому поточному вигляді. Попередні версії, які були відредаговані до початку архівування, відновити неможливо.",
  "backfill.disclaimerApprox":
    "Прогрес і залишок часу — приблизні оцінки на основі кількості повідомлень у Telegram, а не точні значення.",
  "backfill.disclaimerBackground":
    "Після запуску процес завантаження триває на сервері, навіть якщо ви закриєте цю вкладку чи браузер.",
  "backfill.checkingConnection": "Перевірка з'єднання…",
  "backfill.connectionOn": "Архівування зараз підключено",
  "backfill.connectionOff": "Архівування зараз відключено",
  "backfill.startButton": "Почати імпорт історії",
  "backfill.confirmTitle": "Почати імпорт історії?",
  "backfill.confirmBody":
    "Це заархівує історичні повідомлення для вибраного чату (чатів). Для великих чатів це може зайняти багато часу.",
  "backfill.warningConnectionOn":
    "Схоже, що архіватор досі підключено. Зупиніть його перед запуском імпорту історії.",
  "backfill.chatLabel": "Чат (необов'язково — залиште порожнім для всіх чатів)",
  "backfill.chatPlaceholder": "@username або числовий ID",
  "backfill.limitLabel": "Ліміт повідомлень на чат (необов'язково)",
  "backfill.limitPlaceholder": "напр. 500",
  "backfill.confirmStart": "Почати",
  "backfill.stateRunning": "Виконується",
  "backfill.stateCompleted": "Завершено",
  "backfill.stateCancelled": "Скасовано",
  "backfill.stateError": "Помилка",
  "backfill.chats": "чатів",
  "backfill.eta": "Залишилось приблизно",
  "backfill.cancel": "Скасувати",
  "backfill.historyTitle": "Історія запусків",
  "backfill.noHistory": "Імпортування історії ще не було.",
  "backfill.historyStarted": "Розпочато",
  "backfill.historyStatus": "Статус",
  "backfill.historyChats": "Чатів",
  "backfill.historyStored": "Збережено",
  "backfill.historySkipped": "Пропущено",
  "backfill.historyDuration": "Тривалість",

  "messages.wholeWordLabel": "Ціле слово",
  "common.cancel": "Скасувати",
};
