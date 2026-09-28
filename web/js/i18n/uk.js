/**
 * Ukrainian (uk) translation strings.
 * Keep keys in sync with en.js - see the note there.
 */

export const uk = {
  "app.wordmark": "TeleVault",

  "nav.chats": "Чати",
  "nav.messages": "Повідомлення",
  "nav.deleted": "Видалені",
  "nav.stats": "Статистика",
  "nav.health": "Стан системи",
  "nav.settings": "Налаштування",

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
  "deleted.actor.unknown": "Хто видалив - невідомо",
  "deleted.confidence.channel_admin":
    "Лише адміністратор каналу може видалити допис у каналі - звичайні підписники не можуть видаляти дописи.",
  "deleted.confidence.self":
    "Збережені повідомлення доступні лише вам - ніхто інший їх не бачить, а тим паче не може щось із них видалити.",

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
  "health.dbNotSetUp": "Базу даних архіву ще не налаштовано",
  "health.sessionExists": "Сесію Telegram знайдено",
  "health.sessionNotApplicable":
    "Сесію Telegram має лише той обліковий запис, який налаштував підключення для цього інстансу.",
  "health.messageCount": "Заархівовано повідомлень",
  "health.unattached":
    "Ваш архів ще не налаштовано. Завершіть підключення облікового запису Telegram, щоб почати архівування.",
  "health.unavailable":
    "Ваш архів тимчасово недоступний. Спробуйте пізніше або зверніться до адміністратора.",
  "health.refresh": "Оновити",

  "theme.toggleLabel": "Змінити тему",
  "lang.selectLabel": "Мова",

  // Backfill additions
  "nav.backfill": "Імпорт історії",

  "backfill.aboutTitle": "Про імпорт історії",
  "backfill.aboutIntro":
    "Архіватор записує лише повідомлення, надіслані під час його роботи. Імпорт історії заповнює прогалину: він проходить наявну історію кожного чату в Telegram і архівує все, чого TeleVault ще не бачив - корисно одразу після першого налаштування або для будь-якого чату, до якого TeleVault приєднався нещодавно.",
  "backfill.disclaimerSession":
    "Для імпорту історії потрібне окреме з'єднання з Telegram. Спершу зупиніть архівацію (main.py) - Telegram дозволяє лише одну активну сесію одночасно.",
  "backfill.disclaimerDeleted":
    "Повідомлення, видалені ще до того, як чат почав архівуватися, відновити неможливо - API історії Telegram повертає лише ті дані, які існують на даний момент.",
  "backfill.disclaimerEdits":
    "Завантажені повідомлення зберігаються лише в їхньому поточному вигляді. Попередні версії, які були відредаговані до початку архівування, відновити неможливо.",
  "backfill.disclaimerApprox":
    "Прогрес і залишок часу - приблизні оцінки на основі кількості повідомлень у Telegram, а не точні значення.",
  "backfill.disclaimerBackground":
    "Після запуску процес завантаження триває на сервері, навіть якщо ви закриєте цю вкладку чи браузер.",
  "backfill.checkingConnection": "Перевірка з'єднання…",
  "backfill.connectionOn": "Архівування зараз підключено",
  "backfill.connectionOff": "Архівування зараз відключено",
  "backfill.blockedNote": "- імпорт історії недоступний, доки воно працює.",
  "backfill.startButton": "Почати імпорт історії",
  "backfill.confirmTitle": "Почати імпорт історії?",
  "backfill.confirmBody":
    "Це заархівує історичні повідомлення для вибраного чату (чатів). Для великих чатів це може зайняти багато часу.",
  "backfill.warningConnectionOn":
    "Схоже, що архіватор досі підключено. Зупиніть його перед запуском імпорту історії.",
  "backfill.chatLabel": "Чат (необов'язково - залиште порожнім для всіх чатів)",
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
  "backfill.historyChats": "Нових чатів",
  "backfill.historyStored": "Збережено",
  "backfill.historySkipped": "Пропущено",
  "backfill.historyDuration": "Тривалість",
  "backfill.unitHour": "год",
  "backfill.unitMinute": "хв",
  "backfill.unitSecond": "с",

  "messages.wholeWordLabel": "Ціле слово",
  "common.cancel": "Скасувати",

  "archiver.running": "Архіватор працює",
  "archiver.stopped": "Архіватор зупинено",
  "archiver.starting": "Запуск…",
  "archiver.stopping": "Зупинення…",
  "archiver.startAction": "Запустити",
  "archiver.stopAction": "Зупинити",
  "archiver.confirmStop":
    "Зупинити архіватор? Архівування призупиниться, доки ви не запустите його знову.",

  "error.alreadyRunning": "Він уже запущений.",
  "error.notRunning": "Зараз він не запущений.",
  "error.archiverConnected":
    "Архіватор зараз підключений. Спочатку зупиніть його - імпорт історії та архіватор не можуть використовувати одну сесію Telegram одночасно.",
  "error.backfillRunning":
    "Зараз виконується імпорт історії. Спочатку зупиніть його - він та архіватор не можуть використовувати одну сесію Telegram одночасно.",
  "error.archiveUnattached":
    "Ваш архів ще не налаштовано. Завершіть підключення облікового запису Telegram, щоб почати архівування.",
  "error.archiveUnavailable":
    "Ваш архів тимчасово недоступний. Спробуйте пізніше або зверніться до адміністратора.",
  "error.notInstanceOwner":
    "Цей обліковий запис не керує підключенням Telegram цього інстансу.",
  "error.dbUnavailable":
    "База даних тимчасово недоступна. Спробуйте пізніше або зверніться до адміністратора.",

  "chatFilter.allChats": "Усі чати",
  "chatFilter.nChatsSelected": "Вибрано чатів: {n}",
  "chatFilter.searchPlaceholder": "Пошук чатів…",
  "chatFilter.noMatches": "Немає чатів, що відповідають пошуку.",

  // Auth additions
  "nav.logout": "Вийти",

  "login.subtitle": "Увійдіть у свій архів",
  "login.usernameLabel": "Ім'я користувача",
  "login.passwordLabel": "Пароль",
  "login.submit": "Увійти",
  "login.loggingIn": "Вхід…",
  "login.error": "Щось пішло не так. Спробуйте ще раз.",
  "login.registerPrompt": "Маєте запрошення?",
  "login.registerLink": "Створити акаунт",

  "register.subtitle": "Створення акаунта",
  "register.inviteLabel": "Токен запрошення",
  "register.usernameLabel": "Ім'я користувача",
  "register.passwordLabel": "Пароль",
  "register.confirmLabel": "Підтвердіть пароль",
  "register.submit": "Створити акаунт",
  "register.creating": "Створення акаунта…",
  "register.error": "Щось пішло не так. Спробуйте ще раз.",
  "register.passwordMismatch": "Паролі не збігаються.",
  "register.loginPrompt": "Вже маєте акаунт?",
  "register.loginLink": "Увійти",

  // Settings tab (web/js/views/settings.js) - lean by design, just links out to the dedicated Telegram/archive setup page below.
  "settings.telegramCardTitle": "Підключення Telegram і архів",
  "settings.telegramCardBody":
    "Керуйте акаунтом Telegram, який архівує TeleVault, і базою даних, де він зберігається.",
  "settings.telegramCardLink": "Відкрити налаштування Telegram",

  // telegram-setup.html - виділена сторінка першого запуску
  "tgSetup.pageTitle": "Налаштування Telegram",
  "tgSetup.pageSubtitle":
    "Два кроки: підключіть акаунт Telegram для архівування, потім створіть базу даних для зберігання.",
  "tgSetup.telegramTitle": "Підключіть свій акаунт Telegram",
  "tgSetup.telegramStatusChecking": "Перевірка стану підключення Telegram…",
  "tgSetup.telegramIntro":
    "TeleVault архівує повідомлення, використовуючи ваші власні API-дані Telegram, а не спільний застосунок - це повністю відокремлює ліміти та доступ вашого акаунта від інших.",
  "tgSetup.apiIdLabel": "API ID",
  "tgSetup.apiHashLabel": "API hash",
  "tgSetup.guideIntro":
    "Вам потрібні API ID та API hash від самого Telegram. Це займе кілька хвилин:",
  // Назви елементів на сторінці Telegram (API development tools, App title, Short name, Save changes) навмисно лишаються англійською -
  // саме їх користувач шукає на сторінці перед очима.
  "tgSetup.guideStep1":
    "Відкрийте {siteLink} і увійдіть за номером телефону вашого акаунта Telegram. Telegram надішле код підтвердження - введіть його там.",
  "tgSetup.guideStep2":
    "Після входу оберіть {appsLink}. Інші пункти (вийти, видалити акаунт) ігноруйте.",
  "tgSetup.guideStep3":
    'Заповніть поля "App title" і "Short name" - підійде будь-що, це лише підписи для вас. Решту полів залиште як є.',
  "tgSetup.guideStep4": 'Натисніть "Save changes".',
  "tgSetup.guideStep5":
    "Скопіюйте api_id та api_hash з тієї сторінки у поля нижче.",
  "tgSetup.credentialsStoredNote":
    "Зберігається зашифровано; після збереження більше не показується.",
  "tgSetup.credentialsSubmit": "Зберегти й продовжити",
  "tgSetup.savingCredentials": "Збереження…",
  "tgSetup.phoneLabel": "Номер телефону",
  "tgSetup.phonePlaceholder": "+15551234567",
  "tgSetup.phoneHelp":
    "Разом з кодом країни. Ніде не зберігається - використовується лише для запиту цього одноразового коду підтвердження.",
  "tgSetup.sendCodeSubmit": "Надіслати код",
  "tgSetup.sendingCode": "Надсилання…",
  "tgSetup.useDifferentCredentials": "Використати інші дані",
  "tgSetup.codeLabel": "Код підтвердження",
  "tgSetup.codeHelp": "Telegram надіслав код на {phone}.",
  "tgSetup.codeSubmit": "Підтвердити",
  "tgSetup.verifying": "Перевірка…",
  "tgSetup.passwordLabel": "Пароль Telegram (2FA)",
  "tgSetup.passwordHelp":
    "Цей акаунт має увімкнену двофакторну автентифікацію - введіть пароль Telegram, щоб завершити підключення.",
  "tgSetup.passwordSubmit": "Завершити підключення",
  "tgSetup.startOver": "Почати спочатку",
  "tgSetup.linkedBadge": "Підключено",
  "tgSetup.linkedBody":
    "Ваш акаунт Telegram підключено для цієї сесії. Повторне підключення замінить збережену сесію на нову.",
  "tgSetup.relinkButton": "Підключити інший акаунт",
  "tgSetup.archiveTitle": "База даних архіву",
  "tgSetup.archiveIntro":
    "Кожен акаунт отримує власну базу даних для зберігання архівованих повідомлень, окрему від усіх інших акаунтів цього застосунку.",
  "tgSetup.archiveStatusChecking": "Перевірка стану архіву…",
  "tgSetup.archiveStatusReady":
    "Архів готовий - збережено повідомлень: {count}.",
  "tgSetup.archiveStatusMissing": "Базу даних архіву ще не створено.",
  "tgSetup.archiveStatusUnavailable":
    "Архів зареєстровано, але зараз недоступний. Спробуйте пізніше або зверніться до адміністратора.",
  "tgSetup.provisionButton": "Створити базу даних архіву",
  "tgSetup.provisioning": "Створення…",
  "tgSetup.refreshStatus": "Оновити",
  "tgSetup.allSetBody":
    "Telegram підключено, і ваша база даних архіву готова. Тепер можете перейти до чатів.",
  "tgSetup.continueToChats": "Перейти до чатів",
  "tgSetup.skipForNow": "Пропустити - перейти до чатів",

  "error.invalidApiCredentials":
    "Пара api_id/api_hash недійсна. Перевірте значення на my.telegram.org.",
  "error.telegramCredentialsMissing":
    "Спочатку збережіть свої API-дані Telegram.",
  "error.telegramInvalidPhone": "Цей номер телефону недійсний.",
  "error.telegramFloodWait":
    "Telegram просить зачекати. Спробуйте ще раз за кілька хвилин.",
  "error.telegramNoPendingLink":
    "Термін дії цієї спроби підтвердження минув. Надсилаємо новий код.",
  "error.telegramTooManyAttempts": "Забагато спроб. Надсилаємо новий код.",
  "error.telegramPasswordRequired":
    "Цьому акаунту потрібен пароль Telegram, щоб завершити підключення.",
  "error.telegramCodeRequired": "Потрібен код підтвердження.",
  "error.telegramInvalidPassword": "Пароль неправильний.",
  "error.telegramInvalidCode": "Код неправильний.",
  "error.telegramCodeExpired": "Термін дії коду минув. Надсилаємо новий.",
  "error.archiveAlreadyProvisioned": "У цього акаунта вже є база даних архіву.",
  "error.provisioningPermissionDenied":
    "Роль бази даних, під якою підключається TeleVault, не має прав на створення баз даних. Зверніться до адміністратора.",
};
