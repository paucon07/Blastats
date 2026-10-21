// pager buttons
// each one carries data-action and data-page so the caller only registers one action
export function paginationHtml(page, totalPages, action) {
  if (totalPages <= 1) return '';

  const cell = (label, target, { disabled = false, active = false } = {}) => {
    if (active) return `<span class="page-btn active">${label}</span>`;
    if (disabled) return `<span class="page-btn disabled">${label}</span>`;
    return `<button class="page-btn" data-action="${action}" data-page="${target}">${label}</button>`;
  };

  // first, last and one either side of the current page
  const wanted = new Set([1, totalPages]);
  for (let i = page - 1; i <= page + 1; i++) {
    if (i >= 1 && i <= totalPages) wanted.add(i);
  }

  const nums = [];
  let prev = 0;
  for (const n of [...wanted].sort((a, b) => a - b)) {
    // dots where pages were skipped
    if (n - prev > 1) nums.push('<span class="page-ellipsis">...</span>');
    nums.push(cell(n, n, { active: n === page }));
    prev = n;
  }

  return cell('‹ PREV', page - 1, { disabled: page <= 1 }) +
         `<div class="page-nums">${nums.join('')}</div>` +
         cell('NEXT ›', page + 1, { disabled: page >= totalPages });
}
