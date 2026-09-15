/* Страница заказа опрашивает статус, пока платёж не станет окончательным.
   Источник истины — сервер: он обновляет заказ только по подписанному
   уведомлению агрегатора или по ответу его API. */
(function () {
  var card = document.getElementById('order-card');
  if (!card || card.dataset.poll !== 'yes') return;

  var orderId = card.dataset.orderId;
  var attempts = 0;
  var MAX_ATTEMPTS = 120; // ~5 минут при шаге 2.5 с
  var INTERVAL_MS = 2500;

  function tick() {
    attempts += 1;
    if (attempts > MAX_ATTEMPTS) return;

    fetch('/api/orders/' + orderId + '/status', { headers: { Accept: 'application/json' } })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) { return schedule(); }
        if (data.status === 'created' || data.status === 'pending') { return schedule(); }
        window.location.reload();
      })
      .catch(schedule);
  }

  function schedule() { window.setTimeout(tick, INTERVAL_MS); }

  schedule();
})();
