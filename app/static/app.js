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

/* Форма заказа: показываем введённый ID разбитым по три цифры и счётчик длины.
   Исправить ID после оплаты нельзя, поэтому опечатка должна бросаться в глаза. */
(function () {
  var input = document.getElementById('customer_ref');
  var preview = document.getElementById('customer_ref_preview');
  if (!input || !preview) return;

  var EXPECTED = 15;

  function render() {
    var digits = input.value.replace(/\D/g, '');
    if (!digits) { preview.textContent = ''; preview.className = 'ref-preview'; return; }

    var grouped = digits.replace(/(\d{3})(?=\d)/g, '$1 ');
    var complete = digits.length === EXPECTED;
    preview.textContent = grouped + ' — ' + digits.length + ' из ' + EXPECTED;
    preview.className = 'ref-preview' + (complete ? ' ref-preview-ok' : '');
  }

  input.addEventListener('input', render);
  render();
})();
