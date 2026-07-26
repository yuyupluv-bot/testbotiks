ALTER TABLE users
ADD COLUMN IF NOT EXISTS subscription_rules_sent BOOLEAN NOT NULL DEFAULT FALSE;

INSERT INTO settings (key, value) VALUES
('community_rules', 'Правила сообщества:
1. Указывайте точные адреса.
2. Уважайте водителей и пассажиров.
3. Отменяйте неактуальные заявки своевременно.'),
('msg_subscription_required', 'Вы должны быть подписаны на сообщество: {link}'),
('msg_subscription_still_required', 'Вы ещё не подписаны на сообщество: {link}'),
('msg_subscription_check_error', 'Не удалось проверить подписку. Убедитесь, что вы подписались на сообщество, и нажмите «Я подписался» ещё раз: {link}'),
('btn_check_subscription', 'Я подписался'),
('msg_freight_contact_dispatcher', 'По поводу грузоперевозок обращайтесь к диспетчеру с 7:00 до 21:00.')
ON CONFLICT (key) DO NOTHING;
