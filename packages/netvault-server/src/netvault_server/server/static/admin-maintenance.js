(() => {
  const root = document.querySelector('[data-maintenance-console]');
  if (!root) return;
  const initialRevision = root.dataset.maintenanceRevision || '';
  const workerInitiallyOnline = root.dataset.workerOnline === 'true';
  let polling = false;

  async function checkTasks() {
    if (root.isConnected === false) return;
    if (polling || document.visibilityState !== 'visible') return;
    // Keep typed approval/stop confirmations intact during progress updates.
    const confirmations = root.querySelectorAll?.('input[name="confirmation"]') || [];
    if (Array.from(confirmations).some((input) => input.value.trim())) return;
    polling = true;
    try {
      const response = await fetch(root.dataset.stateUrl, {
        method: 'GET',
        credentials: 'same-origin',
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return;
      const state = await response.json();
      if (String(state.revision || '') !== initialRevision
          || Boolean(state.worker_online) !== workerInitiallyOnline) {
        window.location.reload();
      }
    } catch {
      // The host queue continues independently of temporary network errors.
    } finally {
      polling = false;
    }
  }

  window.setInterval(checkTasks, 15000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') void checkTasks();
  });
})();
