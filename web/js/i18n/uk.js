/**
 * Ukrainian (uk) translation strings.
 * Keep keys in sync with en.js - see the note there.
 */

export const uk = {
  "app.wordmark": "TeleVault",
  "app.adminBadge": "Адмін",

  "nav.chats": "Чати",
  "nav.messages": "Повідомлення",
  "nav.deleted": "Видалені",
  "nav.stats": "Статистика",
  "nav.health": "Стан системи",
  "nav.settings": "Налаштування",
  "nav.admin": "Адміністрування",
  "nav.ariaLabel": "Головна навігація",
  "nav.collapse": "Згорнути бічну панель",
  "nav.expand": "Розгорнути бічну панель",

  "common.comingSoon": "Цей розділ ще не готовий.",
  "common.loading": "Завантаження…",
  "common.error": "Щось пішло не так.",
  "common.createArchive": "Створити базу даних архіву",
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
  "health.no": "Ні",
  "health.yes": "Так",
  "health.sessionExists": "Сесію Telegram знайдено",
  "health.messageCount": "Заархівовано повідомлень",
  "health.unattached": "Базу даних вашого архіву ще не створено.",
  "health.unavailable":
    "Ваш архів тимчасово недоступний. Спробуйте пізніше або зверніться до адміністратора.",
  "health.misconfigured":
    "Посилання на вашу базу даних архіву неправильне, і це неможливо виправити повторною спробою. Негайно зверніться до адміністратора.",
  "health.refresh": "Оновити",

  "theme.toggleLabel": "Змінити тему",
  "lang.selectLabel": "Мова",
  "lang.toggleLabel": "Змінити мову",

  // Backfill additions
  "nav.backfill": "Імпорт історії",

  "backfill.aboutTitle": "Про імпорт історії",
  "backfill.aboutIntro":
    "Архіватор записує лише повідомлення, надіслані під час його роботи. Імпорт історії заповнює прогалину: він проходить наявну історію кожного чату в Telegram і архівує все, чого TeleVault ще не бачив - корисно одразу після першого налаштування або для будь-якого чату, до якого TeleVault приєднався нещодавно.",
  "backfill.disclaimerSession":
    "Для імпорту історії потрібне окреме з'єднання з Telegram. Спершу зупиніть архівацію - Telegram дозволяє лише одну активну сесію одночасно.",
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
  "error.archiveUnattached": "Базу даних вашого архіву ще не створено.",
  "error.archiveUnavailable":
    "Ваш архів тимчасово недоступний. Спробуйте пізніше або зверніться до адміністратора.",
  "error.archiveMisconfigured":
    "Посилання на вашу базу даних архіву неправильне, і це неможливо виправити повторною спробою. Негайно зверніться до адміністратора.",
  "error.telegramNotLinked":
    "Спершу підключіть свій обліковий запис Telegram (Налаштування → Налаштування Telegram).",
  "error.notAdmin": "Ця дія потребує прав адміністратора.",
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
  "register.usernameHint": "3-32 символи: літери, цифри, . _ -",
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
  "settings.blocklistTitle": "Чати без архівування",
  "settings.blocklistIntro":
    "Повідомлення та редагування в чатах із цього списку більше не архівуються. Усі інші чати, зокрема й будь-який новий, архівуються, доки ви не додасте їх сюди. Уже заархівовані повідомлення залишаються як є. Зміна діє з наступного повідомлення, перезапуск не потрібен. Усе, що надходить, поки чат заблоковано, не зберігається взагалі, тож його не можна відновити, якщо ви пізніше розблокуєте чат.",
  "settings.blocklistChatLabel": "Чат, який більше не архівувати",
  "settings.blocklistPick": "Оберіть чат…",
  "settings.blocklistAdd": "Не архівувати",
  "settings.blocklistAdding": "Блокування…",
  "settings.blocklistRemove": "Архівувати знову",
  "settings.blocklistRemoving": "Розблокування…",
  "settings.blocklistEmpty": "Жоден чат не заблоковано - архівуються всі чати.",
  "settings.blocklistNoChats": "Поки що немає заархівованих чатів для вибору.",
  "settings.blocklistUnnamed": "Чат без назви",
  "settings.blocklistBlockedOn": "Заблоковано",
  "settings.devicesTitle": "Пристрої Telegram",
  "settings.devicesIntro":
    "Усі пристрої, на яких виконано вхід в ваш акаунт Telegram - той самий список, що в Налаштування → Пристрої самого Telegram. Щоразу, коли ви підключаєте TeleVault, додається новий запис, тож старі можуть накопичуватися. Щоб завантажити список, TeleVault ненадовго підключається до Telegram, тому спершу зупиніть архіватор та імпорт історії.",
  "settings.devicesLoad": "Показати мої пристрої",
  "settings.devicesRefresh": "Оновити",
  "settings.devicesEmpty": "Telegram не повернув жодного пристрою.",
  "settings.deviceThisSession": "TeleVault (ця сесія)",
  "settings.deviceSignedIn": "Вхід виконано",
  "settings.deviceLastActive": "Остання активність",
  "settings.deviceEnd": "Завершити сеанс",
  "settings.deviceEnding": "Завершення…",
  "settings.deviceUnnamed": "Невідомий пристрій",
  "settings.deviceEndConfirm":
    "Завершити сеанс «{device}»? Його буде виведено з вашого акаунта Telegram, і доведеться входити знову.",
  "settings.devicesEndOthers": "Завершити всі інші сеанси",
  "settings.devicesEndOthersConfirm":
    "Завершити всі сеанси, крім власного сеансу TeleVault? Усі інші ваші пристрої (телефони, комп'ютери, інші застосунки) буде виведено з Telegram, і на них доведеться входити знову.",

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
  "tgSetup.codeHelp": "Telegram прийняв запит для {phone}.",
  "tgSetup.codeHelpApp":
    'Telegram надіслав код повідомленням у застосунку Telegram, у чаті з назвою "Telegram" (це не SMS). Відкрийте його на телефоні чи комп\'ютері.',
  "tgSetup.codeHelpSms": "Telegram надіслав код у SMS на {phone}.",
  "tgSetup.codeHelpCall": "Telegram зателефонує на {phone} і продиктує код.",
  "tgSetup.codeHelpEmail":
    "Telegram вимагає підтвердження через e-mail, якого TeleVault виконати не може, тому цей номер зараз неможливо підключити.",
  "tgSetup.codeSubmit": "Підтвердити",
  "tgSetup.verifying": "Перевірка…",
  "tgSetup.passwordLabel": "Пароль Telegram (2FA)",
  "tgSetup.passwordHelp":
    "Цей акаунт має увімкнену двофакторну автентифікацію - введіть пароль Telegram, щоб завершити підключення.",
  "tgSetup.passwordSubmit": "Завершити підключення",
  "tgSetup.startOver": "Почати спочатку",
  "tgSetup.qrButton": "Увійти за QR-кодом",
  "tgSetup.relinkNote":
    "Ви замінюєте поточне підключення. Воно працює, доки нове не буде підключене, а «Скасувати» залишає його без змін. У будь-якому разі стару сесію НЕ буде завершено автоматично: вона залишиться серед ваших пристроїв Telegram, доки ви її не завершите - на вкладці «Налаштування» в TeleVault (Пристрої Telegram) або в Telegram: Налаштування → Пристрої.",
  "tgSetup.relinkCredentialsNote":
    "Збереження нових API-даних вимкне підключення цього акаунта до TeleVault, доки ви знову не підключите Telegram (архіватор у цей час працювати не зможе). «Скасувати» залишає поточне підключення. Сесія також залишається у списку пристроїв Telegram, а після очищення тут TeleVault більше не зможе її завершити - тому, якщо хочете її прибрати, спершу завершіть її (вкладка «Налаштування» → Пристрої Telegram).",
  "tgSetup.unlinkNotEnded":
    "Telegram відключено тут, але TeleVault не зміг завершити свою сесію всередині Telegram. Видаліть її самостійно в Telegram: Налаштування → Пристрої.",
  "tgSetup.cancelRelink": "Скасувати - залишити поточне підключення",
  "tgSetup.relinkingBar": "Ви перепідключаєте акаунт Telegram.",
  "tgSetup.deviceNote":
    "Зверніть увагу: щоразу, коли ви підключаєте акаунт - навіть той самий на тому самому комп'ютері - Telegram додає новий запис у Налаштування → Пристрої. Попередні автоматично не завершуються; ви можете завершити їх на вкладці «Налаштування» в TeleVault (Пристрої Telegram) або в самому Telegram.",
  "tgSetup.qrPrimaryHint":
    "Найшвидший спосіб: відскануйте QR-код у Telegram на телефоні. Код підтвердження не потрібен.",
  "tgSetup.phoneAlternative":
    "Або скористайтеся номером телефону та кодом підтвердження",
  "tgSetup.qrIntro":
    "Скануйте цей код у Telegram на телефоні. Код входу не потрібен.",
  "tgSetup.qrStep1": "Відкрийте Telegram на телефоні.",
  "tgSetup.qrStep2":
    "Перейдіть у Налаштування → Пристрої → Під’єднати пристрій.",
  "tgSetup.qrStep3":
    "Наведіть камеру на код. Він оновлюється сам приблизно кожні 30 секунд.",
  "tgSetup.qrWarning":
    "Скануйте лише той QR-код, який ви самі створили на цій сторінці. Той, хто відскануює чужий код, віддає доступ до свого акаунта.",
  "tgSetup.qrAlt": "QR-код для входу в Telegram",
  "tgSetup.qrLoading": "Готуємо QR-код…",
  "tgSetup.qrCancel": "Назад",
  "tgSetup.qrExpired": "QR-код застарів або спроба не вдалася. Почніть знову.",
  "tgSetup.linkedBadge": "Підключено",
  "tgSetup.linkedBody":
    "Ваш акаунт Telegram підключено для цієї сесії. Повторне підключення замінить збережену сесію на нову. Стару сесію не буде завершено автоматично (див. Налаштування → Пристрої Telegram).",
  "tgSetup.relinkButton": "Підключити інший акаунт",
  "tgSetup.unlinkButton": "Відключити Telegram",
  "tgSetup.unlinking": "Відключення…",
  "tgSetup.unlinkConfirm":
    "Відключити цей акаунт Telegram? Заархівовані повідомлення збережуться - ви можете підключитися знову будь-коли, з тими самими API-даними. TeleVault також завершує свою сесію в Telegram, тож вона зникне з Налаштування → Пристрої.",
  "tgSetup.archiveTitle": "База даних архіву",
  "tgSetup.archiveIntro":
    "Кожен акаунт отримує власну базу даних для зберігання архівованих повідомлень, окрему від усіх інших акаунтів цього застосунку.",
  "tgSetup.archiveStatusChecking": "Перевірка стану архіву…",
  "tgSetup.archiveStatusReady":
    "Архів готовий - збережено повідомлень: {count}.",
  "tgSetup.archiveStatusMissing": "Базу даних архіву ще не створено.",
  "tgSetup.archiveStatusUnavailable":
    "Архів додано, але зараз він недоступний. Спробуйте пізніше або зверніться до адміністратора.",
  "tgSetup.archiveStatusMisconfigured":
    "Архів додано, але базу даних так і не було створено. Це неможливо виправити повторною спробою - негайно зверніться до адміністратора.",
  "tgSetup.provisionButton": "Створити базу даних архіву",
  "tgSetup.provisioning": "Створення…",
  "tgSetup.refreshStatus": "Оновити",
  "tgSetup.allSetBody":
    "Telegram підключено, і ваша база даних архіву готова. Тепер можете перейти до чатів.",
  "tgSetup.continueToChats": "Перейти до чатів",
  "tgSetup.skipForNow": "Пропустити - перейти до чатів",

  "error.invalidInput":
    "Деякі введені значення недійсні. Перевірте довжину та дозволені символи й спробуйте ще раз.",
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
  "error.telegramQrNotScanned": "Спочатку відскануйте QR-код у Telegram.",
  "error.telegramSessionInvalid":
    "Telegram більше не приймає збережену сесію TeleVault (її завершено деінде). Підключіть акаунт Telegram знову.",
  "error.telegramSessionUnreadable":
    "Збережену сесію Telegram неможливо прочитати. Підключіть акаунт Telegram знову.",
  "error.telegramSessionTooNew":
    "Telegram не дозволяє сесії завершувати інші пристрої, доки їй не мине приблизно доба. Спробуйте пізніше.",
  "error.telegramDeviceNotFound": "Цього пристрою вже немає в Telegram.",
  "error.telegramCannotEndCurrent":
    "Це власна сесія TeleVault. Щоб завершити її, скористайтеся «Відключити Telegram».",
  "error.telegramUnreachable":
    "Зараз не вдається зв'язатися з Telegram. Спробуйте трохи пізніше.",
  "error.stopArchiverFirst":
    "Спочатку зупиніть архіватор (і імпорт історії, якщо він запущений) - вони використовують цю сесію Telegram.",
  "error.telegramCodeExpired": "Термін дії коду минув. Надсилаємо новий.",
  "error.archiveOutdated":
    "Базу даних вашого архіву потрібно оновити, перш ніж блокувати чати. Попросіть того, хто обслуговує цей TeleVault, застосувати оновлення бази (python scripts/migrate_archives.py).",
  "error.blocklistFull":
    "Список заблокованих чатів заповнений. Спершу розблокуйте якийсь чат.",
  "error.chatNotFound": "Цього чату немає у вашому архіві.",
  "error.archiveAlreadyProvisioned": "У цього акаунта вже є база даних архіву.",
  "error.provisioningPermissionDenied":
    "Роль бази даних, під якою підключається TeleVault, не має прав на створення баз даних. Зверніться до адміністратора.",

  // Admin panel (web/js/views/admin.js)
  "admin.usersCardTitle": "Користувачі",
  "admin.roleAdmin": "Адмін",
  "admin.statusLocked": "Заблоковано",
  "admin.youLabel": "(ви)",
  "admin.adminLabel": "Обліковий запис адміна",
  "admin.lockButton": "Заблокувати",
  "admin.unlockButton": "Розблокувати",
  "admin.deleteButton": "Видалити",
  "admin.tgLinked": "Telegram підключено",
  "admin.tgNotLinked": "Telegram не підключено",
  "admin.archiveSet": "Архів налаштовано",
  "admin.archiveMissing": "Без архіву",
  "admin.createdLabel": "Створено",
  "admin.lastLoginLabel": "Останній вхід",
  "admin.lockConfirm":
    "Заблокувати цей обліковий запис? Користувач не зможе увійти, доки ви його не розблокуєте.",
  "admin.unlockConfirm": "Розблокувати цей обліковий запис?",
  "admin.invitesCardTitle": "Запрошення",
  "admin.invitesCardBody":
    "Створіть одноразовий токен запрошення для реєстрації. Передайте його напряму - будь-хто, у кого він є, може зареєструватися до завершення терміну дії.",
  "admin.expiresHoursLabel": "Діє протягом (годин)",
  "admin.createInviteButton": "Створити запрошення",
  "admin.creatingInvite": "Створення…",
  "admin.noInvitesYet": "Запрошень ще не створено.",
  "admin.inviteCreatedLabel":
    "Запрошення створено - скопіюйте зараз, повторно воно не відображатиметься:",
  "admin.inviteExpiresLabel": "Діє до",
  "admin.inviteUnused": "Ще не використано",
  "admin.inviteUsedBy": "Використав(ла)",
  "admin.inviteUsedByDeleted": "(обліковий запис видалено)",
  "admin.deleteConfirmTitle": "Видалення облікового запису",
  "admin.deleteConfirmMessage":
    "Це остаточно видалить обліковий запис, його базу даних архіву та підключення Telegram.",
  "admin.searchUsersPlaceholder": "Пошук користувачів за іменем…",
  "admin.searchInvitesPlaceholder": "Пошук запрошень за іменем або #id…",
  "admin.noMatches": "Нічого не знайдено.",
  "admin.inviteStatusUsed": "Використано",
  "admin.inviteStatusExpired": "Прострочено",
  "admin.inviteStatusActive": "Активне",
  "admin.inviteExpiredOnLabel": "Прострочено",
  "admin.inviteDeleteButton": "Видалити",
  "admin.inviteDeleteConfirm":
    "Видалити це запрошення? Його токен одразу перестане працювати. Використані запрошення видалити не можна.",
  // Shared in-app dialogs (web/js/lib/dialog.js)
  "dialog.ok": "OK",
  "dialog.confirm": "Підтвердити",
  "dialog.typedInputLabel": 'Введіть "{value}" для підтвердження',
  "dialog.typedBlank": "Введіть текст точно так, як показано, щоб продовжити.",
  "dialog.typedMismatch":
    "Не збігається. Перевірте написання та регістр літер.",
  // Сторінка 404 (web/404.html)
  "notFound.title": "Цю сторінку не заархівовано",
  "notFound.body":
    "За цією адресою нічого немає. На відміну від ваших повідомлень, цю сторінку так і не було збережено.",
  "notFound.button": "Повернутися до архіву",
  "admin.copyButton": "Копіювати",
  "admin.copied": "Скопійовано",
  "admin.copyManual": "Виділено - натисніть Ctrl+C",
  "auth.invalidInput":
    "Деякі введені значення неприпустимі. Перевірте їх і спробуйте ще раз.",
  // Футер / юридичні сторінки
  "app.releaseStage": "Закрите бета-тестування",
  "legal.tosLink": "Умови користування",
  "legal.privacyLink": "Політика конфіденційності",
  "legal.backHome": "← Назад до TeleVault",
  "legal.pageNote":
    "Цей документ наведено англійською мовою. Якщо колись з'явиться переклад, переважну силу матиме англомовна версія.",
  // Прапорець згоди при реєстрації - текст посилань стоїть у реченні, тому потрібна окрема граматична форма
  "register.termsPrefix": "Я погоджуюся з ",
  "register.termsTosLink": "Умовами користування",
  "register.termsMiddle": " та підтверджую ознайомлення з ",
  "register.termsPrivacyLink": "Політикою конфіденційності",
  // Помилки входу / реєстрації (коди причин з api/routes/auth.py)
  "error.invalidCredentials": "Неправильне ім'я користувача або пароль.",
  "error.loginRateLimitedIp":
    "Забагато невдалих спроб входу з цієї адреси. Спробуйте пізніше.",
  "error.loginRateLimitedAccount":
    "Забагато невдалих спроб входу для цього облікового запису. Спробуйте пізніше.",
  "error.accountLocked":
    "Цей обліковий запис заблоковано. Зверніться до адміністратора.",
  "error.adminExists":
    "Обліковий запис адміністратора вже існує. Попросіть запрошення в адміністратора.",
  "error.usernameTaken": "Це ім'я користувача вже зайняте.",
  "error.inviteInvalid":
    "Цей токен запрошення недійсний, прострочений або вже використаний.",
  "error.termsNotAccepted":
    "Щоб створити обліковий запис, потрібно погодитися з Умовами користування та підтвердити ознайомлення з Політикою конфіденційності.",
};
