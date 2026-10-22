# Мутанты `worker.py`: разбор выживших

Вход: mutmut 3.8.0, прогон по всему `src/first_contact/` на коммите `befbd6b`. Группа «worker» —
`src/first_contact/worker.py`: 489 выживших мутантов на разбор и 383, признанных эквивалентными
автоматически (`text_literal` 350, `error_message` 9, `logging` 24). Повторного прогона mutmut не было.

## Как проверено, что тест краснеет

Каждый мутант (все 872, включая автоматически признанные) применён к **копии** `worker.py` и прогнан узкий набор
группы — `tests/test_worker.py tests/test_send_outcome.py tests/test_first_send.py` (`pytest -x`), в
docker-контейнере `python:3.11`, по копии дерева на мутант во временной папке контейнера.
Файлы продукта в рабочем дереве не менялись. Дифф mutmut задан по функции (отступ от `def`), поэтому он
применялся сценарием, который находит функцию по AST и заменяет строки хунка; для всех 872 проверено, что
итоговый дифф копии против исходника совпадает с диффом mutmut построчно. До мутантов — контрольный прогон
исходного кода на той же копии: зелёный, импорт `first_contact` — из копии.

«Убит» ниже — узкий набор краснеет на мутанте; в колонке теста — первый упавший. Все эти мутанты выживали в
прогоне mutmut на коммите `befbd6b` (весь набор тестов), так что краснеют они из-за тестов этой работы.

## Сводка по 489 мутантам на разбор

| Вердикт | Мутантов |
|---|---|
| пробел — закрыт усилением тестов | 272 |
| пробел — не закрыт (эталона нет, вопрос человеку) | 2 |
| эквивалентный | 31 |
| непродуктивный | 184 |
| **всего** | 489 |

Что считалось контрактом (эталон — README, `docs/РАЗРАБОТЧИКУ.md`, `docs/НАСТРОЙКА.md`, `docs/УСТАНОВКА.md`):
статус и поля сделки, видимые в карточке (имя, телефон, канал, «Попыток с ошибкой», наличие причины в
«Комментарии» — текст причины не контракт), вызовы Битрикса и Орбиты (кому, с каким ключом, телом, именем),
ключи идемпотентности, факт комментария в сделке (README: «каждый шаг робот отмечает комментарием»; текст — нет),
запись журнала — там, где документ её называет: «ждём телефон» (один раз), «мессенджер подтвердил доставку»,
«статус … не меняется», причина `message.failed`, «Орбита: тестовое событие», события канала, «нет
подтверждения отправки», «idempotency_key_reused», «пятый сбой подряд — запись в журнал». Границы сроков
проверены ровно на границе часами, остановленными на секунде (фикстура `frozen` в `tests/test_worker.py`):
секундная точность меток в базе не даёт попасть на границу движущимися часами `clock`.

## Таблица

Номера — `__mutmut_N` функции. Тест — тот, что теперь краснеет на мутантах строки.

| Функция | Мутанты | Вердикт | Обоснование | Каким тестом убит |
|---|---|---|---|---|
| `UnknownOutcome.__init__` | 1 | пробел, закрыт | причина неизвестного исхода не доходит до журнала: «idempotency_key_reused» в журнале — НАСТРОЙКА.md | `test_worker.py::test_first_message_key_reused_needs_check` |
| `_after_outcome` | 3 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_apply_first` | 7 | пробел, закрыт | исход прежней отправки меняет статус no_messenger/skipped (таблица «Исход отправки по request_key») | `test_send_outcome.py::test_late_confirmation_does_not_undo_decision_of_new_attempt` |
| `_apply_first` | 30, 31, 55, 57, 58, 59, 60, 63, 64, 103, 105, 106, 107, 108 | пробел, закрыт | журнал исхода: причина отказа, «мессенджер подтвердил доставку», «статус … не меняется» (РАЗРАБОТЧИКУ.md); значения needs_check/reason_code | `test_send_outcome.py::test_outcome_recorded_in_deal_journal` |
| `_apply_first` | 46, 48 | пробел, закрыт | message.failed: причина не попадает в last_error («причина — в карточке», НАСТРОЙКА.md) | `test_send_outcome.py::test_failure_reason_shown_in_card` |
| `_apply_first` | 69, 70, 71 | пробел, закрыт | исход применён, а ключ не снят (инвариант «исход применён ⇒ ключ снят») | `test_send_outcome.py::test_repeated_confirmation_of_recorded_first_message_releases_key` |
| `_apply_first` | 87 | пробел, закрыт | подтверждение без sent_at/pending_at или без dialog_id: наивное время (#87) ломает напоминание, ключ колонки (#76) — падение | `test_send_outcome.py::test_confirmation_without_time_or_dialog_then_reminder` |
| `_apply_first` | 91 | пробел, закрыт | подтверждение стирает канал отправки в карточке | `test_worker.py::test_handed_over_first_message_keeps_number_and_channel_it_went_to` |
| `_apply_first` | 117 | пробел, закрыт | после подтверждения в карточке остаётся «нет подтверждения» | `test_worker.py::test_late_message_sent_after_timeout_confirms_first` |
| `_apply_first` | 51, 137, 166 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_apply_first` | 121, 123, 124, 125, 126 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `_apply_first` | 161 | непродуктивный | текст «как узнали» / «кто нажал» в журнале — формулировка |  |
| `_apply_step` | 67 | пробел, закрыт | у подтверждённого ответа не снимается reply_due_at (РАЗРАБОТЧИКУ.md); значение шага "reply" | `test_worker.py::test_late_confirmation_between_retry_and_resend_is_applied` |
| `_apply_step` | 85 | пробел, закрыт | после подтверждения в карточке остаётся «нет подтверждения» | `test_worker.py::test_late_confirmation_after_timeout_returns_deal` |
| `_apply_step` | 89, 91, 92, 93, 94, 95, 96, 98, 101, 104, 105, 154, 156, 157, 158, 159, 160, 161 | пробел, закрыт | журнал исхода напоминания/ответа и значение needs_check в условии | `test_send_outcome.py::test_outcome_recorded_in_deal_journal` |
| `_apply_step` | 113 | пробел, закрыт | номер повтора шага не растёт дальше -r1 | `test_worker.py::test_failure_of_retried_step_gives_next_key` |
| `_apply_step` | 137, 139 | пробел, закрыт | причина отказа не попадает в last_error | `test_send_outcome.py::test_failure_reason_shown_in_card` |
| `_apply_step` | 70 | эквивалентный | "" и None одинаково ложны везде, где читается reply_due_at; replies_due строку с reply_sent_at не берёт |  |
| `_apply_step` | 6, 7, 8, 11 | непродуктивный | заголовок шага и подробности записей, документом не названных, — формулировка |  |
| `_apply_step` | 61 | непродуктивный | момент из события задан документом только для первого сообщения; для напоминания и ответа эталона нет (см. «Пробелы документа») |  |
| `_apply_step` | 164 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_client_replied` | 2 | непродуктивный | текст «как узнали» / «кто нажал» в журнале — формулировка |  |
| `_client_replied` | 6 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_connected` | 5 | пробел, не закрыт | эталона нет — см. «Дефекты и вопросы» | — |
| `_expired` | 15 | пробел, закрыт | граница 48 ч: ровно 48 ч — ещё не «старше» | `test_worker.py::test_deal_exactly_48h_old_still_handled` |
| `_expired` | 4 | эквивалентный | created_at NOT NULL, DATE_CREATE передаётся только непустым — max не бывает пустым |  |
| `_first_done` | 7 | пробел, закрыт | 201: канал отправки не записывается в карточку | `test_worker.py::test_first_message_substitutes_name_and_title` |
| `_first_done` | 31, 32, 40, 41, 42 | пробел, закрыт | 202: номер/канал отправки и сброс прежней причины в карточке | `test_worker.py::test_handed_over_first_message_keeps_number_and_channel_it_went_to` |
| `_first_done` | 53 | пробел, закрыт | исход, принятый до ключа, теряет комментарий в сделку | `test_send_outcome.py::test_outcome_received_before_key_comments_first_message` |
| `_first_done` | 44, 45 | эквивалентный | intent_phone / intent_channel читаются только вместе с intent_key, а он снят той же записью |  |
| `_first_done` | 13 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_first_done` | 29 | непродуктивный | заголовок шага и подробности записей, документом не названных, — формулировка |  |
| `_first_sent` | 12, 16 | пробел, закрыт | канал не записывается в карточку | `test_worker.py::test_first_message_substitutes_name_and_title` |
| `_first_sent` | 19, 20 | эквивалентный | intent_phone / intent_channel читаются только вместе с intent_key, а он снят той же записью |  |
| `_first_sent` | 21 | эквивалентный | intent_key после 201 не читается: first_contact идёт только по new, а сделку с first_sent_at «Повторить» в new не вернёт |  |
| `_handed_over` | 12 | эквивалентный | _match_outcome(None) ищет ключ 'None' — такого request_key нет |  |
| `_handed_over` | 5, 7, 8, 9, 10 | непродуктивный | заголовок шага и подробности записей, документом не названных, — формулировка |  |
| `_mark_sent_first` | 7 | пробел, закрыт | у отправленной сделки остаётся причина прошлого сбоя | `test_worker.py::test_failures_count_and_reset_when_deal_moves` |
| `_mark_sent_first` | 8 | пробел, закрыт | 201 не снимает request_key прежней передачи («ответ 201 его снимает») | `test_worker.py::test_first_message_sent_after_retry_no_longer_waits_previous_handover` |
| `_mark_sent_first` | 15 | пробел, закрыт | канал не записывается в карточку | `test_worker.py::test_first_message_substitutes_name_and_title` |
| `_mark_sent_first` | 9 | эквивалентный | pending_at читается только у pending_first и в сверке до факта доставки; после first_sent_at — нигде |  |
| `_mark_sent_first` | 20, 22, 23, 24, 25 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `_no_messenger` | 13, 16 | пробел, закрыт | нет причины в карточке для no_messenger/no_phone | `test_worker.py::test_no_messenger_moves_deal_to_stage` |
| `_no_messenger` | 17, 19, 20, 21, 22 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `_no_messenger` | 27 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_poll_reply` | 3 | пробел, закрыт | статус под замком не перечитывается (остановленная сделка становится «ответил») | `test_worker.py::test_poll_replies_rechecks_deal_after_waiting_its_turn` |
| `_poll_reply` | 16, 17 | пробел, закрыт | читается история старого диалога или диалога None | `test_worker.py::test_poll_replies_by_button_skips_dialogs_without_fresh_history` |
| `_poll_reply` | 33 | пробел, закрыт | граница «раз в 5 минут» | `test_worker.py::test_poll_replies_at_exactly_five_minutes` |
| `_poll_reply` | 36, 37, 39 | пробел, закрыт | checked_at не пишется — история читается каждый проход | `test_worker.py::test_poll_replies_reads_history_once_per_five_minutes` |
| `_poll_reply` | 42, 44 | пробел, закрыт | старая переписка до первого сообщения считается ответом | `test_worker.py::test_poll_replies_ignores_old_conversation_before_first_message` |
| `_poll_reply` | 46, 48 | эквивалентный | since у history — только где остановить листание ленты; решение принимают client_replied / first_incoming с since — результат тот же, меняется число страниц |  |
| `_poll_reply` | 24 | непродуктивный | граница 3 суток (REPLY_POLL_DAYS) документом не названа — «свежие диалоги»; эталона на границе нет |  |
| `_poll_reply` | 60 | непродуктивный | текст «как узнали» / «кто нажал» в журнале — формулировка |  |
| `_restore_stage` | 3 | пробел, закрыт | в DRY_RUN меняется этап в Битриксе | `test_worker.py::test_stage_not_changed_in_dry_run` |
| `_restore_stage` | 17 | пробел, закрыт | граница паузы возврата: срок наступил — возврат идёт | `test_worker.py::test_stage_return_at_exact_retry_moment` |
| `_restore_stage` | 36, 37, 38 | пробел, закрыт | работа по этапу не закрывается — Битрикс опрашивается снова | `test_worker.py::test_retry_does_not_move_deal_the_manager_already_moved` |
| `_restore_stage` | 53, 54, 55, 56, 78, 79, 80, 102, 103, 105, 111, 112, 114, 116, 117, 118, 121, 122, 123, 124, 125, 127, 128, 129, 130, 132, 133, 134, 136, 137, 138, 139, 140, 141, 142, 143 | пробел, закрыт | паузы 2/4/8/16, счёт сбоев, сдача на пятом с журналом и комментарием «верните вручную», непредвиденная ошибка | `test_worker.py::test_stage_return_paused_2_4_8_16_minutes_then_given_up` |
| `_restore_stage` | 60, 61, 62, 63, 64, 68 | пробел, закрыт | срок лимита Битрикса не соблюдается или сбой в обработке лимита тратит попытку сделки | `test_worker.py::test_stage_return_waits_limit_named_by_bitrix` |
| `_restore_stage` | 113 | пробел, закрыт | permanent-ошибка не сдаётся сразу (имя атрибута, getattr) | `test_worker.py::test_stage_return_given_up_at_once_when_retry_cannot_help` |
| `_restore_stage` | 30, 31, 32 | эквивалентный | stage_attempts / stage_retry_at после закрытия работы (stage_before снят) не читаются; второго цикла возврата у сделки нет — на «Не нашёл мессенджер» робот уводит только до первого отправленного |  |
| `_restore_stage` | 115 | эквивалентный | отсутствующий атрибут даёт None, ложный так же, как False |  |
| `_restore_stage` | 39, 40, 41, 42, 43, 44, 67, 69, 70, 71, 72, 104, 106, 107, 149, 151, 152, 153, 154 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `_restore_stage` | 89, 90, 91, 96, 97, 98, 101 | непродуктивный | аргументы и условие записи в лог; 96 и 97 краснеют только под pytest (его обработчик логов падает на ошибке формата, в работе logging её глотает) |  |
| `_restore_stage` | 126, 159 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `_resume_first` | 25 | пробел, закрыт | намерение не снимается при определённом исходе — повтор по кругу | `test_worker.py::test_unfinished_send_found_not_sent_then_ordinary_rules` |
| `_resume_first` | 50 | пробел, закрыт | после повтора намерения в карточке нет номера | `test_worker.py::test_lost_result_write_keeps_intent_retry_goes_to_same_channel` |
| `_retry` | 50 | пробел, закрыт | «Повторить» не обнуляет счётчик сбоев | `test_worker.py::test_five_failures_in_a_row_is_error` |
| `_retry` | 69 | пробел, закрыт | followup_retry_n не растёт | `test_worker.py::test_retry_after_failed_followup_does_not_resend_first` |
| `_retry` | 104 | пробел, закрыт | reply_retry_n не растёт | `test_worker.py::test_retry_sends_old_undelivered_template_reply` |
| `_retry` | 117 | пробел, закрыт | retry_n не растёт: второй «Повторить» снова -r1 | `test_worker.py::test_each_retry_of_first_message_gets_next_key` |
| `_retry` | 124, 129 | пробел, закрыт | намерение не снимается из needs_check (или снимается из error) | `test_worker.py::test_first_message_key_reused_needs_check` |
| `_retry` | 80 | пробел, не закрыт | эталона нет — см. «Дефекты и вопросы» | — |
| `_retry` | 127, 128 | эквивалентный | intent_phone / intent_channel читаются только вместе с intent_key, а он снят той же записью |  |
| `_retry` | 133, 135, 136, 137, 138 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `_send_outcome` | 17, 25, 26, 27 | пробел, закрыт | reason_code теряется (значение ключа события Орбиты) | `test_send_outcome.py::test_outcome_recorded_in_deal_journal` |
| `_send_outcome` | 42 | непродуктивный | аргументы и условие записи в лог; 96 и 97 краснеют только под pytest (его обработчик логов падает на ошибке формата, в работе logging её глотает) |  |
| `_send_step` | 16 | пробел, закрыт | напоминание уходит не в диалог сделки | `test_worker.py::test_followup_when_client_silent` |
| `_send_step` | 24 | пробел, закрыт | «idempotency_key_reused» не доходит до журнала | `test_worker.py::test_key_reused_needs_check_and_retry_goes_with_new_key` |
| `_send_step` | 29, 30, 31 | пробел, закрыт | после определённого отказа намерение остаётся — «Повторить» идёт старым ключом | `test_worker.py::test_definite_refusal_of_reminder_lets_retry_send_new_key` |
| `_set_replied` | 17 | пробел, закрыт | пауза ответа по шаблону вычитается вместо прибавления | `test_worker.py::test_reply_by_template_waits_pause_from_settings` |
| `_set_replied` | 29 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `_start` | 18, 23, 24 | пробел, закрыт | имя контакта не уходит в поле name запроса | `test_first_send.py::test_retry_after_lost_answer_repeats_whole_original_request` |
| `_start` | 26, 30 | пробел, закрыт | после 4xx намерение остаётся — следующая попытка не смотрит свежие данные | `test_worker.py::test_definite_refusal_lets_next_attempt_decide_on_fresh_data` |
| `_start` | 28, 29, 40, 41 | эквивалентный | intent_phone / intent_channel читаются только вместе с intent_key, а он снят той же записью |  |
| `_too_old` | 9, 12, 18 | пробел, закрыт | нет причины пропуска в карточке; падение журнала тратит попытку | `test_worker.py::test_age_counted_from_deal_creation_in_bitrix` |
| `_too_old` | 17, 19, 20, 21, 22 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `after_followup` | 27, 29, 39 | пробел, закрыт | старая переписка до первого сообщения считается ответом | `test_worker.py::test_outcome_after_followup` |
| `auto_reply` | 3, 4, 5 | пробел, закрыт | состояние под замком не перечитывается — ответ уходит | `test_worker.py::test_template_reply_rechecks_deal_after_waiting_its_turn` |
| `auto_reply` | 18, 19, 20 | пробел, закрыт | выключенный ответ остаётся в плане / сбой | `test_worker.py::test_reply_by_template_turned_off_cancels_planned_reply` |
| `auto_reply` | 36 | пробел, закрыт | граница 48 ч повтора незавершённого ответа | `test_worker.py::test_unfinished_template_reply_repeated_exactly_48h_after_due` |
| `auto_reply` | 54, 56, 57 | пробел, закрыт | старое входящее до первого сообщения считается ответом клиента (опоздание) | `test_worker.py::test_reply_by_template_ignores_old_conversation_before_first_message` |
| `auto_reply` | 61, 62, 63, 65 | пробел, закрыт | план ответа не снимается — история читается снова / сбой | `test_worker.py::test_reply_by_template_not_needed_without_client_message` |
| `auto_reply` | 82 | пробел, закрыт | падение записи журнала тратит попытку | `test_worker.py::test_no_template_reply_when_manager_answered` |
| `auto_reply` | 103 | пробел, закрыт | граница «опоздал больше чем на 30 минут» | `test_worker.py::test_template_reply_exactly_30_minutes_late_still_sent` |
| `auto_reply` | 108 | пробел, закрыт | падение записи журнала тратит попытку | `test_worker.py::test_no_template_reply_when_client_wrote_long_ago` |
| `auto_reply` | 129, 134, 153, 188, 189, 190, 193, 195, 196 | пробел, закрыт | имя в тексте ответа, комментарий об ответе, сброс счётчика | `test_worker.py::test_reply_by_template_sent_once_after_pause` |
| `auto_reply` | 148 | пробел, закрыт | после отправки ответа счётчик сбоев не сброшен | `test_worker.py::test_reply_by_template_after_failure_clears_failure_count` |
| `auto_reply` | 175, 177, 178 | пробел, закрыт | нет комментария о переданном ответе | `test_worker.py::test_handed_over_template_reply_noted` |
| `auto_reply` | 182 | пробел, закрыт | отказ, принятый до ключа, теряет комментарий | `test_send_outcome.py::test_failure_received_before_key_comments_template_reply` |
| `auto_reply` | 185 | пробел, закрыт | 201 не снимает reply_request_key — карточка «ждём подтверждения» | `test_worker.py::test_template_reply_sent_after_retry_no_longer_waits_confirmation` |
| `auto_reply` | 31 | эквивалентный | reply_retried_at не бывает позже reply_due_at: «Повторить» пишет оба одним моментом, позже сдвигается только reply_due_at |  |
| `auto_reply` | 44, 46, 49 | эквивалентный | since у history — только где остановить листание ленты; решение принимают client_replied / first_incoming с since — результат тот же, меняется число страниц |  |
| `auto_reply` | 64, 66, 67, 68, 69, 81, 83, 84, 85, 86, 107, 109, 110, 111, 112, 187 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `auto_reply` | 121, 162, 173 | непродуктивный | заголовок шага и подробности записей, документом не названных, — формулировка |  |
| `auto_reply` | 176, 194 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `disable` | 1 | эквивалентный | None ложно везде, где читается enabled, и сохраняется как «выключен» |  |
| `first_contact` | 3 | пробел, закрыт | статус не проверяется — второе первое сообщение | `test_worker.py::test_repeated_bitrix_event_does_not_send_first_message_again` |
| `first_contact` | 21, 22 | пробел, закрыт | после повтора намерения строка сделки теряется — падение | `test_worker.py::test_handed_over_first_message_keeps_number_and_channel_it_went_to` |
| `first_contact` | 23, 25, 29 | пробел, закрыт | 48 ч от created_at не проверяются / статус не пишется | `test_worker.py::test_deal_seen_more_than_48h_ago_skipped_without_creation_date` |
| `first_contact` | 45, 48, 52 | пробел, закрыт | нет причины пропуска; падение журнала тратит попытку | `test_worker.py::test_only_selected_funnel` |
| `first_contact` | 76, 79, 83 | пробел, закрыт | нет причины пропуска; падение журнала тратит попытку | `test_worker.py::test_deal_on_no_messenger_stage_untouched_without_manual_retry` |
| `first_contact` | 91 | пробел, закрыт | контакты читаются не этой сделки | `test_worker.py::test_first_message_substitutes_name_and_title` |
| `first_contact` | 106 | пробел, закрыт | граница PHONE_WAIT | `test_worker.py::test_no_phone_exactly_ten_minutes_after_creation` |
| `first_contact` | 107, 113, 114, 115, 117, 118, 121, 122 | пробел, закрыт | «ждём телефон»: причина в карточке и одна запись журнала (РАЗРАБОТЧИКУ.md) | `test_worker.py::test_contact_bound_after_deal_created_gets_message` |
| `first_contact` | 128, 129, 264, 265 | пробел, закрыт | no_phone: нет причины, прежний этап не запоминается | `test_worker.py::test_stage_returned_after_retry_of_deal_without_valid_phone` |
| `first_contact` | 182, 184, 187, 294 | пробел, закрыт | нет номера и причины в карточке no_messenger | `test_worker.py::test_no_messenger_moves_deal_to_stage` |
| `first_contact` | 213 | пробел, закрыт | неподключённый канал обрывает перебор каналов | `test_worker.py::test_disconnected_channel_skipped_next_channel_tried` |
| `first_contact` | 278, 280 | пробел, закрыт | срок берётся, хотя его назвали не все каналы | `test_worker.py::test_wait_named_by_only_some_unchecked_channels_spends_attempt` |
| `first_contact` | 183, 186 | эквивалентный | имя той же попыткой перезаписывает _start на каждом номере |  |
| `first_contact` | 51, 53, 54, 55, 56, 82, 84, 85, 86, 87, 120, 123, 237, 239, 240, 241, 242, 253, 255, 256, 257, 258 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `first_contact` | 138, 139, 140, 141, 142, 143, 144, 145, 146, 149, 150, 151, 152, 153, 154, 155, 156, 157, 159, 160, 161, 162, 163, 164, 169, 170, 171, 172, 173, 174 | непродуктивный | ветка DRY_RUN: документ задаёт только «ничего не отправлять и не менять в Битриксе» — мутанты этого не нарушают; статус и поля сделки в DRY_RUN не заданы (см. «Пробелы документа») |  |
| `first_contact` | 212, 282 | непродуктивный | меняется только текст ошибки в last_error / журнале; попытка тратится так же |  |
| `mark_replied` | 11 | пробел, закрыт | «Клиент ответил» не снимает план ответа | `test_worker.py::test_client_replied_button_cancels_planned_template_reply` |
| `mark_replied` | 27 | пробел, закрыт | панель получает «сделки нет» для существующей | `test_worker.py::test_manual_action_reports_whether_deal_is_in_robot` |
| `mark_replied` | 19, 21, 22, 23, 24 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `on_deal_added` | 8 | пробел, закрыт | сделка из базы отсекается границей | `test_worker.py::test_bitrix_event_for_deal_already_in_robot_processed_at_once` |
| `on_deal_added` | 11 | пробел, закрыт | граница включения «не больше» | `test_worker.py::test_bitrix_event_for_boundary_deal_ignored` |
| `on_deal_added` | 22 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `on_orbita_event` | 18, 19 | пробел, закрыт | строки «Орбита: тестовое событие» в журнале нет (НАСТРОЙКА.md) | `test_worker.py::test_test_event_changes_nothing` |
| `on_orbita_event` | 50, 51, 52, 53, 66 | пробел, закрыт | message.received переводит в «ответил» сделки, которые ответа не ждут | `test_worker.py::test_message_received_ignored_where_robot_waits_no_reply` |
| `on_orbita_event` | 85 | пробел, закрыт | событие без request_key или чужого диалога — падение обработки | `test_worker.py::test_event_matching_no_deal_changes_nothing` |
| `on_orbita_event` | 101, 106, 107, 108, 109, 110, 111, 113, 114 | пробел, закрыт | событие канала не видно в журнале / какой канал (НАСТРОЙКА.md); значения типов событий и provider | `test_worker.py::test_channel_event_visible_in_journal` |
| `on_orbita_event` | 3, 5 | эквивалентный | у события без type ни None, ни "" не совпадают ни с одним типом |  |
| `on_orbita_event` | 17, 20 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `on_orbita_event` | 29, 31 | непродуктивный | message.received без direction: поведение документом не задано |  |
| `on_orbita_event` | 78 | непродуктивный | текст «как узнали» / «кто нажал» в журнале — формулировка |  |
| `on_orbita_event` | 112, 117 | непродуктивный | подстановка id канала, когда нет provider, — форма подробностей журнала, документом не названа |  |
| `pending_timeout` | 3 | пробел, закрыт | статус под замком не перечитывается | `test_worker.py::test_pending_timeout_rechecks_deal_after_waiting_its_turn` |
| `pending_timeout` | 15 | пробел, закрыт | граница «дольше 30 минут» | `test_worker.py::test_pending_exactly_30_minutes_still_waits` |
| `pending_timeout` | 18, 21, 27, 28, 30, 31, 39, 41, 42 | пробел, закрыт | таймаут: причина в карточке, «нет подтверждения отправки» в журнале (УСТАНОВКА.md), комментарий | `test_worker.py::test_late_message_sent_after_timeout_confirms_first` |
| `pending_timeout` | 29, 32, 35 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `pending_timeout` | 40 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `poll_replies` | 3, 5 | пробел, закрыт | sent_followup не опрашивается | `test_worker.py::test_poll_replies_also_after_reminder` |
| `poll_replies` | 1 | эквивалентный | единственный вызов передаёт force явно |  |
| `reconnect` | 4, 8 | пробел, закрыт | клиент создаётся без ключа — проверка связи зовёт сервис | `test_worker.py::test_status_page_does_not_call_unconfigured_services` |
| `reconnect` | 9, 10, 11, 12, 13, 14 | пробел, закрыт | адрес/ключ/каналы Орбиты не из настроек | `test_worker.py::test_orbita_client_follows_saved_settings` |
| `retry` | 2 | непродуктивный | текст «как узнали» / «кто нажал» в журнале — формулировка |  |
| `send_timeout` | 28 | пробел, закрыт | таймаут напоминания срабатывает после ответа клиента | `test_worker.py::test_reminder_timeout_irrelevant_once_client_replied` |
| `send_timeout` | 38 | пробел, закрыт | граница «30 минут» | `test_worker.py::test_unconfirmed_step_exactly_30_minutes_still_waits` |
| `send_timeout` | 41, 44, 47, 48, 50, 51, 53, 55, 56 | пробел, закрыт | таймаут: причина, журнал, комментарий | `test_worker.py::test_unconfirmed_202_needs_check_after_timeout_and_retry_keeps_key` |
| `send_timeout` | 49, 52 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `send_timeout` | 54 | непродуктивный | текст комментария в сделку (факт комментария остаётся) |  |
| `stop` | 17, 21 | пробел, закрыт | нет причины остановки в карточке | `test_worker.py::test_stop_records_reason` |
| `stop` | 18, 23 | пробел, закрыт | нет признака ручной остановки — поздний отказ планирует ответ | `test_worker.py::test_stop_after_unconfirmed_reply_survives_late_failure` |
| `stop` | 22 | пробел, закрыт | «Остановить» не снимает план ответа | `test_worker.py::test_stop_cancels_template_reply_of_failing_deal` |
| `stop` | 36 | пробел, закрыт | панель получает «сделки нет» для существующей | `test_worker.py::test_manual_action_reports_whether_deal_is_in_robot` |
| `stop` | 28, 30, 31, 32, 33 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |
| `take` | 7, 9, 10, 11, 12 | непродуктивный | запись журнала, документом не названная: привязка к сделке, название, подробности |  |

## Дефекты продукта и вопросы, вскрытые мутантами

Продукт не правился. Тестом эти места не закрыты — эталона нет, решение за человеком.

1. **`_connected` #5 — канал намерения при отключённых всех каналах.** Документ: «Канал намерения сейчас не
   подключён → `needs_check`». Код: если не подключён ни один канал из настроек, `_resume_first` падает раньше —
   `_connected` бросает `OrbitaError`, и сделка повторяет попытки (пять — `error`), а не уходит в `needs_check`.
   Мутант (`&` → `|`) снимает эту проверку и ведёт себя по букве документа. Что верно — повтор (временное
   отключение всех каналов) или `needs_check` — документ не различает. В новой отправке разница мутанта только
   в имени и телефоне в карточке до отказа — это не контракт.
2. **`_retry` #80 (и связанное с ним поведение `stop`) — «Повторить» после «Остановить» не возобновляет ответ по
   шаблону.** Сценарий: клиент ответил, ответ по шаблону раз за разом не уходит → `error`; оператор нажал
   «Остановить» (план ответа снят — это теперь проверяет `test_stop_cancels_template_reply_of_failing_deal`),
   затем «Повторить». Сделка возвращается в `replied`, но ответ по шаблону не планируется: в коде он планируется,
   только если `reply_due_at` ещё стоит. Документ: «ответ по шаблону (`reply_retry_n`, возврат в `replied`, если
   ответ не ушёл, — он планируется заново)» и «Решение человека — продолжить: ручная остановка снимается». По
   букве документа ответ должен планироваться; мутант (`and` → `or`) так и делает. Без «Остановить» это состояние
   недостижимо: остальные пути, снимающие план («Клиент ответил», выключенный ответ, «ответ не нужен» по истории),
   оставляют статус `replied`, который «Повторить» не берёт. Нужен ответ: дефект кода или
   неточность документа.
3. **Мелкое расхождение: ответ по шаблону, остановленный оператором, и поздний `message.sent`.** Документ: при
   ручном решении — «только факт и запись в журнал («статус … не меняется»)». Код для ответа по шаблону после
   «Остановить» пишет «мессенджер подтвердил доставку» без «не меняется»: статус `replied` совпадает со статусом
   ожидания шага. Статус и правда не меняется; тестом этот случай не закреплён (`JOURNAL_CASES` в
   `tests/test_send_outcome.py` его не содержит).

### Пробелы документа (мутанты признаны непродуктивными из-за отсутствия эталона)

- **DRY_RUN:** документ задаёт только «ничего не отправлять и не менять в Битриксе». Какой статус и поля получает
  сделка в DRY_RUN (сейчас `skipped` с «DRY_RUN»), не сказано — 30 мутантов ветки (`first_contact` 138–174) и
  7 автоматических проверять не по чему. Нарушение самого «не менять в Битриксе» (возврат этапа) — проверено
  (`test_stage_not_changed_in_dry_run`).
- **Время подтверждения напоминания и ответа:** для первого сообщения документ называет «момент из события»,
  для `followup_sent_at` / `reply_sent_at` — нет (`_apply_step` #61).
- **«Свежие диалоги» опроса истории:** граница 3 суток (`REPLY_POLL_DAYS`) в документе не названа (`_poll_reply`
  #24); что совсем старый диалог не опрашивается, проверено на 10 сутках.
- **`message.received` без `direction`** и **событие канала без `provider`** — поведение не задано.

## Эквивалентные автоматически — перепроверка

Все 383 автоматически признанных мутанта прогнаны тем же способом. Класс `text_literal` оказался неоднороден: в
`worker.py` строками заданы и тексты, и значения протокола (статусы, имена колонок, типы событий Орбиты, шаги,
`timespec`, имена атрибутов). **Из «эквивалентных автоматически» исключено 57** — как пробелы
(закрыты, краснеют) или непродуктивные; ниже — по классам.

| Класс | Всего | Итог перепроверки |
|---|---|---|
| `text_literal` | 350 | непродуктивный — 12; пробел, закрыт — 44; текст / лог (подтверждено) — 171; эквивалентный — 2; эквивалентный (лог; краснеет только под pytest) — 1; эквивалентный: регистр имени колонки в SQL — 15; эквивалентный: регистр имени колонки в sqlite3.Row — 105 |
| `error_message` | 9 | пробел, закрыт — 1; текст / лог (подтверждено) — 8 |
| `logging` | 24 | текст / лог (подтверждено) — 19; эквивалентный (лог; краснеет только под pytest) — 5 |

Обоснование эквивалентности по классам (тест-гайд, §5 «На что тест не пишется» и часть II «Как читать
результат»):

- **текст** (сообщения журнала, комментарии в Битрикс, `last_error` для человека, текст исключения, строки лога):
  точная формулировка — не контракт (§5, «Точную формулировку сообщения…», «Дословный текст записи в журнал»);
  факт записи и комментария остаётся. Исключение — названия записей журнала, которые документ приводит
  дословно («ждём телефон», «Орбита: тестовое событие», «нет подтверждения отправки», «мессенджер подтвердил
  доставку»): их мутанты-заглавные краснеют и учтены как пробелы; мутанты, оставляющие фразу внутри строки
  (`"XX…XX"`), фразу не теряют.
- **регистр имени колонки в `sqlite3.Row`** (`row["STATUS"]`): доступ к `sqlite3.Row` по имени
  регистронезависим — поведение то же.
- **регистр имени колонки в SQL** (ключи полей `db.update`): имена колонок SQLite регистронезависимы; там, где
  `DB.update` дописывает сброс `attempts` / `next_attempt_at`, повтор колонки в `SET` даёт то же значение
  (проверено на SQLite: последнее присваивание).
- **лог**: строки и аргументы `log.*` — не контракт. Пять мутантов `logging` и один `text_literal`
  (`_restore_stage` 85, 92–95, 100: неверный формат) краснеют только под pytest — его обработчик логов поднимает
  ошибку форматирования, которую `logging` в работе глотает; в эксплуатации поведение то же.

Исключённые из автоматических — пробелы, теперь краснеют:

| Класс | Функция | Мутанты | Тест |
|---|---|---|---|
| text_literal | `_apply_first` | 43, 44, 110 | `test_outcome_recorded_in_deal_journal` |
| text_literal | `_apply_first` | 76 | `test_confirmation_without_time_or_dialog_then_reminder` |
| text_literal | `_apply_step` | 68, 69 | `test_late_confirmation_between_retry_and_resend_is_applied` |
| text_literal | `_apply_step` | 124, 125 | `test_outcome_recorded_in_deal_journal` |
| text_literal | `_poll_reply` | 10, 11 | `test_poll_replies_also_after_reminder` |
| text_literal | `_restore_stage` | 65, 66, 75, 76, 77 | `test_stage_return_waits_limit_named_by_bitrix` |
| text_literal | `_restore_stage` | 81, 144, 145 | `test_stage_return_paused_2_4_8_16_minutes_then_given_up` |
| text_literal | `_restore_stage` | 119, 120 | `test_stage_return_given_up_at_once_when_retry_cannot_help` |
| text_literal | `_resume_first` | 48 | `test_retry_after_lost_answer_for_contact_without_name` |
| text_literal | `_retry` | 35, 36 | `test_retry_after_reminder_continues_with_outcome` |
| text_literal | `_retry` | 125, 126 | `test_first_message_key_reused_needs_check` |
| text_literal | `_send_outcome` | 28, 29 | `test_outcome_recorded_in_deal_journal` |
| error_message | `_start` | 25 | `test_first_message_key_reused_needs_check` |
| text_literal | `auto_reply` | 137 | `test_reply_by_template_without_name_uses_fallback_greeting` |
| text_literal | `first_contact` | 124, 125 | `test_contact_bound_after_deal_created_gets_message` |
| text_literal | `on_orbita_event` | 21, 22, 23 | `test_test_event_changes_nothing` |
| text_literal | `on_orbita_event` | 102, 103, 104, 105, 115, 116 | `test_channel_event_visible_in_journal` |
| text_literal | `pending_timeout` | 34, 36 | `test_late_message_sent_after_timeout_confirms_first` |
| text_literal | `poll_replies` | 8, 9 | `test_poll_replies_also_after_reminder` |
| text_literal | `render` | 1 | `test_reply_by_template_sent_once_after_pause` |

Исключённые из автоматических — непродуктивные (значения, для которых нет эталона):

| Функция | Мутант | Почему |
|---|---|---|
| `first_contact` | 147 | ветка DRY_RUN |
| `first_contact` | 148 | ветка DRY_RUN |
| `first_contact` | 158 | ветка DRY_RUN |
| `first_contact` | 165 | ветка DRY_RUN |
| `first_contact` | 166 | ветка DRY_RUN |
| `first_contact` | 167 | ветка DRY_RUN |
| `first_contact` | 168 | ветка DRY_RUN |
| `first_contact` | 190 | разделитель номеров в поле «Телефон» карточки — форма отображения |
| `on_orbita_event` | 34 | message.received без direction — поведение документом не задано |
| `on_orbita_event` | 35 | message.received без direction — поведение документом не задано |
| `on_orbita_event` | 118 | подстановка id канала без provider — документом не названа |
| `on_orbita_event` | 119 | подстановка id канала без provider — документом не названа |

Эквивалентные по существу (не текст):

| Функция | Мутант | Почему |
|---|---|---|
| `on_orbita_event` | 8 | у события без type ни "", ни "XXXX" не совпадают ни с одним типом |
| `render (модуль)` | 1 | Worker.render всегда передаёт deal_title |
