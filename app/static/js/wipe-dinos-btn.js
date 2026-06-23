const wipeDinosBtn = document.getElementById('wipe-dinos-btn');
if (wipeDinosBtn) {
  wipeDinosBtn.addEventListener('click', async function () {
    if (!confirm(
      'Wipe ALL wild (untamed) dinos?\n\n' +
      'Players will receive a 10-second warning in chat before the wipe fires.\n\n' +
      'This cannot be undone.'
    )) return;

    wipeDinosBtn.disabled = true;
    wipeDinosBtn.textContent = 'Wiping...';

    try {
      const response = await fetch(`/api/ark/wipe-dinos/${serverId}`, { method: 'POST' });
      const data = await response.json();

      if (!response.ok) {
        showAlert(`Wipe failed: ${data.error}`, 'danger');
      } else {
        showAlert('Wild dino wipe complete.', 'success');
      }
    } catch (error) {
      showAlert(`Wipe failed: ${error.message}`, 'danger');
    } finally {
      wipeDinosBtn.disabled = false;
      wipeDinosBtn.textContent = 'Wipe Wild Dinos';
    }
  });
}
