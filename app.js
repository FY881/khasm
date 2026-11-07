(() => {
  'use strict';

  const categoryLabels = {
    phones: 'موبايلات', laptops: 'لابتوبات', headphones: 'سماعات', tvs: 'تلفزيونات',
    appliances: 'أجهزة منزلية', perfumes: 'عطور', supermarket: 'سوبر ماركت',
    gaming: 'ألعاب', tablets: 'تابلت', fashion: 'أزياء',
  };
  const storeLabels = { noon: 'نون مصر', amazon: 'أمازون مصر' };
  const money = new Intl.NumberFormat('ar-EG', { style: 'currency', currency: 'EGP', maximumFractionDigits: 2 });
  const digits = new Intl.NumberFormat('ar-EG', { maximumFractionDigits: 1 });
  const byId = (id) => document.getElementById(id);
  const watchlistStorageKey = 'khasm-watchlist-v1';
  const dialog = byId('product-dialog');
  const details = byId('detail-content');
  const elements = {
    search: byId('search'), store: byId('store-filter'), category: byId('category-filter'),
    discount: byId('discount-filter'), clear: byId('clear-filters'), results: byId('deals'),
    count: byId('results-count'), message: byId('message-area'),
    update: byId('update-status'), updateTitle: byId('update-title'), updateDetail: byId('update-detail'),
    watchlistToggle: byId('watchlist-toggle'), watchlistStatus: byId('watchlist-status'),
  };
  let products = [];
  let watchlist = new Set();
  let showWatchlistOnly = false;
  let watchlistStorageFailed = false;
  let renderedStaleness = new Map();
  let updatedAt = null;
  let loadError = false;
  let blockedPages = [];
  const relativeTime = new Intl.RelativeTimeFormat('ar-EG', { numeric: 'auto' });

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function safeHttpsUrl(value) {
    if (typeof value !== 'string') return null;
    try {
      const url = new URL(value);
      return url.protocol === 'https:' ? url : null;
    } catch {
      return null;
    }
  }

  function safeStoreUrl(product) {
    const url = safeHttpsUrl(product.url);
    if (!url) return null;
    const host = url.hostname.toLowerCase();
    const allowed = product.store === 'noon'
      ? host === 'noon.com' || host.endsWith('.noon.com')
      : host === 'amazon.eg' || host.endsWith('.amazon.eg');
    return allowed ? url : null;
  }

  function configuredTelegramChannel() {
    const configured = document.querySelector('meta[name="telegram-channel-url"]')?.content.trim();
    const url = safeHttpsUrl(configured);
    if (!url || url.hostname.toLowerCase() !== 't.me' || url.username || url.password || url.port) return null;
    if (!/^\/(?:[A-Za-z0-9_]{5,}|\+[A-Za-z0-9_-]+)\/?$/.test(url.pathname)) return null;
    return url;
  }

  function productsDataUrl() {
    const configured = document.querySelector('meta[name="products-data-url"]')?.content.trim();
    return new URL(configured || 'data/products.json', document.baseURI);
  }

  async function fetchProductsPayload() {
    const localUrl = new URL('data/products.json', document.baseURI);
    const remoteUrl = productsDataUrl();
    let response = await fetch(remoteUrl);
    if (response.status === 404 && remoteUrl.href !== localUrl.href) {
      response = await fetch(localUrl);
    }
    if (!response.ok) throw new Error('Products data request failed');
    return response.json();
  }

  function readWatchlist() {
    try {
      const saved = localStorage.getItem(watchlistStorageKey);
      const keys = saved === null ? [] : JSON.parse(saved);
      if (!Array.isArray(keys)) throw new Error('Invalid saved watchlist');
      watchlist = new Set(keys.filter((key) => typeof key === 'string' && key.length > 0 && key.length <= 500).slice(0, 200));
      watchlistStorageFailed = false;
    } catch {
      watchlist = new Set();
      watchlistStorageFailed = true;
    }
    updateWatchlistStatus();
    updateWatchlistControl();
  }

  function updateWatchlistStatus() {
    elements.watchlistStatus.hidden = !watchlistStorageFailed;
    elements.watchlistStatus.textContent = watchlistStorageFailed
      ? 'المتصفح منع التخزين؛ قائمة المتابعة هتفضل موجودة لحد ما الصفحة تتقفل بس.'
      : '';
  }

  function updateWatchlistControl() {
    elements.watchlistToggle.setAttribute('aria-pressed', String(showWatchlistOnly));
    elements.watchlistToggle.textContent = showWatchlistOnly
      ? `عرض كل المنتجات (${digits.format(watchlist.size)})`
      : `قائمة المتابعة (${digits.format(watchlist.size)})`;
  }

  function saveWatchlist() {
    try {
      localStorage.setItem(watchlistStorageKey, JSON.stringify([...watchlist]));
      watchlistStorageFailed = false;
    } catch {
      watchlistStorageFailed = true;
    }
    updateWatchlistStatus();
  }

  function relativeObservationText(observedAt) {
    const seconds = Math.max(0, Math.floor((Date.now() - observedAt.getTime()) / 1000));
    if (seconds < 60) return 'اتشيك من أقل من دقيقة';
    if (seconds < 3600) return `آخر تحقق للسعر: ${relativeTime.format(-Math.floor(seconds / 60), 'minute')}`;
    if (seconds < 86400) return `آخر تحقق للسعر: ${relativeTime.format(-Math.floor(seconds / 3600), 'hour')}`;
    return `آخر تحقق للسعر: ${relativeTime.format(-Math.floor(seconds / 86400), 'day')}`;
  }

  function normalizeProduct(item) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return null;
    const id = typeof item.id === 'string' || typeof item.id === 'number' ? String(item.id).trim() : '';
    const store = item.store;
    const score = item.score && typeof item.score === 'object' ? item.score : {};
    const currentPrice = Number(item.current_price);
    if (!id || !Object.prototype.hasOwnProperty.call(storeLabels, store) || typeof item.title !== 'string' || !item.title.trim() || !Number.isFinite(currentPrice) || currentPrice <= 0) return null;
    let status = ['new', 'real', 'fake', 'none'].includes(score.status) ? score.status : 'none';
    const discount = Number(score.discount_percent);
    if (status === 'real' && (!Number.isFinite(discount) || discount <= 0)) status = 'none';
    const history = Array.isArray(item.history) ? item.history.flatMap((point) => {
      if (!point || typeof point !== 'object') return [];
      const price = Number(point.price);
      const time = typeof point.date === 'string' ? Date.parse(point.date) : NaN;
      return Number.isFinite(price) && price > 0 && Number.isFinite(time) ? [{ date: new Date(time), price }] : [];
    }).sort((a, b) => a.date - b.date) : [];
    if (status === 'real' && !history.length) status = 'none';
    const listPrice = item.list_price === null || item.list_price === undefined ? null : Number(item.list_price);
    const lowestDays = score.lowest_days === null || score.lowest_days === undefined ? NaN : Number(score.lowest_days);
    const lastCheckedStamp = typeof item.last_checked === 'string' ? Date.parse(item.last_checked) : NaN;
    return {
      id, key: `${store}:${id}`, store, title: item.title.trim(), url: item.url,
      image: item.image, currentPrice, listPrice: Number.isFinite(listPrice) && listPrice > 0 ? listPrice : null,
      seller: typeof item.seller === 'string' ? item.seller : null,
      category: typeof item.category === 'string' && Object.prototype.hasOwnProperty.call(categoryLabels, item.category) ? item.category : null,
      status, discount: Number.isFinite(discount) ? Math.max(0, discount) : 0,
      lowestDays: Number.isInteger(lowestDays) && lowestDays >= 0 ? lowestDays : null,
      history, lastChecked: Number.isFinite(lastCheckedStamp) ? new Date(lastCheckedStamp) : null,
    };
  }

  function normalizeText(value) {
    return String(value).toLocaleLowerCase('ar-EG').normalize('NFKC')
      .replace(/[\u064B-\u065F\u0670]/g, '').replace(/\u0640/g, '').trim();
  }

  function productMatches(product, { query, store, category, minDiscount }) {
    if (store !== 'all' && product.store !== store) return false;
    if (category !== 'all' && product.category !== category) return false;
    if (query && !normalizeText(`${product.title} ${storeLabels[product.store]} ${categoryLabels[product.category] || ''}`).includes(query)) return false;
    if (product.status === 'real' && product.discount < minDiscount) return false;
    if (product.status === 'new' && minDiscount > 0) return false;
    if (product.status === 'fake' && product.discount < minDiscount) return false;
    if (product.status === 'none' && minDiscount > 0) return false;
    return true;
  }

  function currentFilters() {
    return {
      query: normalizeText(elements.search.value), store: elements.store.value,
      category: elements.category.value, minDiscount: Number(elements.discount.value) || 0,
    };
  }

  function formatDate(date, options = {}) {
    return new Intl.DateTimeFormat('ar-EG', { day: 'numeric', month: 'short', ...options }).format(date);
  }

  function productObservedAt(product) {
    return product.lastChecked || (product.history.length ? product.history[product.history.length - 1].date : null);
  }

  function productIsStale(product) {
    const observedAt = productObservedAt(product);
    return !observedAt || Date.now() - observedAt.getTime() > 6 * 3_600_000;
  }

  function observationLabel(product) {
    const observedAt = productObservedAt(product);
    const time = node('time', `observation-note${productIsStale(product) ? ' is-stale' : ''}`, observedAt
      ? relativeObservationText(observedAt)
      : 'وقت آخر رصد مش متاح');
    if (observedAt) {
      time.dateTime = observedAt.toISOString();
      time.title = `آخر تحقق للسعر: ${formatDate(observedAt, { year: 'numeric', hour: 'numeric', minute: '2-digit' })}`;
    }
    return time;
  }

  function refreshObservationLabels() {
    document.querySelectorAll('time.observation-note[datetime]').forEach((time) => {
      const observedAt = new Date(time.dateTime);
      if (!Number.isFinite(observedAt.getTime())) return;
      time.textContent = relativeObservationText(observedAt);
      time.title = `آخر تحقق للسعر: ${formatDate(observedAt, { year: 'numeric', hour: 'numeric', minute: '2-digit' })}`;
      time.classList.toggle('is-stale', Date.now() - observedAt.getTime() > 6 * 3_600_000);
    });
  }

  function updateFreshness() {
    elements.update.classList.remove('is-stale', 'is-error');
    if (loadError) {
      elements.update.classList.add('is-error');
      elements.updateTitle.textContent = 'البيانات مش متاحة دلوقتي';
      elements.updateDetail.textContent = 'جرّب تحدّث الصفحة بعد شوية';
      return;
    }
    if (!updatedAt) {
      elements.update.classList.add('is-stale');
      elements.updateTitle.textContent = 'مفيش تحديث مسجّل لسه';
      elements.updateDetail.textContent = 'هنعرض البيانات لما توصل';
      return;
    }
    const hours = Math.max(0, (Date.now() - updatedAt.getTime()) / 3_600_000);
    if (hours > 6) {
      elements.update.classList.add('is-stale');
      elements.updateTitle.textContent = 'تحديث الأسعار متأخر';
      elements.updateDetail.textContent = `آخر حفظ للبيانات: ${formatDate(updatedAt, { year: 'numeric', hour: 'numeric', minute: '2-digit' })}`;
      return;
    }
    elements.updateTitle.textContent = 'وقت آخر حفظ لملف البيانات';
    elements.updateDetail.textContent = formatDate(updatedAt, { year: 'numeric', hour: 'numeric', minute: '2-digit' });
  }

  function addNotice(text, strongText = '') {
    const notice = node('div', 'notice');
    notice.setAttribute('role', 'status');
    notice.append(node('span', 'notice-icon', '﹅'));
    const paragraph = node('p');
    if (strongText) paragraph.append(node('strong', '', `${strongText} `));
    paragraph.append(document.createTextNode(text));
    notice.append(paragraph);
    elements.message.append(notice);
  }

  function imageOrFallback(parent, product, imageClass = '') {
    const wrap = node('span', imageClass);
    const imageUrl = safeHttpsUrl(product.image);
    if (imageUrl) {
      const image = document.createElement('img');
      image.src = imageUrl.href;
      image.alt = product.title;
      image.loading = 'lazy';
      image.referrerPolicy = 'no-referrer';
      image.addEventListener('error', () => {
        const fallback = node('span', 'image-placeholder', 'خ');
        image.replaceWith(fallback);
      }, { once: true });
      wrap.append(image);
    } else {
      wrap.append(node('span', 'image-placeholder', 'خ'));
    }
    return wrap;
  }

  function productCard(product) {
    const card = node('button', 'product-card');
    const stale = productIsStale(product);
    card.type = 'button';
    card.setAttribute('aria-label', `تفاصيل المنتج: ${product.title}، ${stale ? 'آخر رصد قديم أو غير معروف' : 'بيانات مرصودة حديثًا'}`);
    card.addEventListener('click', () => openProduct(product));
    const image = imageOrFallback(card, product, 'product-image');
    const badge = stale
      ? node('span', 'product-badge stale-badge', '◷ رصد قديم')
      : product.status === 'real'
      ? node('span', 'product-badge', '✓ نزول حقيقي')
      : product.status === 'new'
      ? node('span', 'product-badge new-badge', '◷ جديد — بنتابع')
      : node('span', 'product-badge new-badge', watchlist.has(product.key) ? '♡ في المتابعة' : 'مفيش نزول مؤكد');
    image.append(badge);
    card.append(image);
    const info = node('span', 'product-info');
    const meta = node('span', 'product-meta');
    meta.append(node('span', '', storeLabels[product.store]), node('span', '', categoryLabels[product.category] || 'منتج'));
    info.append(meta, node('span', 'product-title', product.title));
    info.append(node('span', 'price-label', stale ? 'آخر سعر مرصود — اتأكد قبل الشراء' : 'السعر المسجّل'));
    const price = node('span', 'price-row');
    price.append(node('span', 'current-price', money.format(product.currentPrice)));
    if (product.status === 'real' && stale) price.append(node('span', 'new-price-note', `كان أقل بـ ${digits.format(product.discount)}٪ وقت الرصد`));
    else if (product.status === 'real') price.append(node('span', 'discount-value', `−${digits.format(product.discount)}٪`));
    else if (product.status === 'fake') price.append(node('span', 'new-price-note', 'الخصم مش حقيقي'));
    else if (product.status === 'none') price.append(node('span', 'new-price-note', 'مفيش نزول مؤكد دلوقتي'));
    else price.append(node('span', 'new-price-note', 'لسه مفيش مقارنة'));
    info.append(price);
    info.append(observationLabel(product));
    if (product.status === 'real' && !stale && product.lowestDays !== null) {
      info.append(node('span', 'lowest-note', product.lowestDays === 0 ? 'أقل سعر مسجل النهارده' : `أرخص سعر من ${digits.format(product.lowestDays)} يوم`));
    }
    card.append(info);
    return card;
  }

  function sectionHeading(title, count, type) {
    const heading = node('div', 'group-heading');
    const label = node('h3', 'group-title');
    label.append(node('span', 'group-symbol', type === 'real' ? '↓' : '◷'), document.createTextNode(title));
    heading.append(label, node('span', 'count-pill', `${digits.format(count)} منتجات`));
    return heading;
  }

  function emptyState(title, text) {
    const box = node('div', 'empty-state');
    box.append(node('span', 'empty-icon', '⌕'), node('h3', '', title), node('p', '', text));
    return box;
  }

  function openProduct(product, updateUrl = true) {
    if (updateUrl) {
      const url = new URL(window.location.href);
      url.searchParams.set('product', product.key);
      history.pushState({ product: product.key }, '', url);
    }
    renderDetails(product);
    if (!dialog.open) dialog.showModal();
  }

  function renderExcluded(productsToShow) {
    if (!productsToShow.length) return null;
    const disclosure = node('details', 'excluded-details');
    disclosure.append(node('summary', '', `مستبعدين ${digits.format(productsToShow.length)} تنبيه خصم مش حقيقي — للتوضيح`));
    const list = node('div', 'excluded-list');
    productsToShow.forEach((product) => {
      const row = node('div', 'excluded-product');
      row.append(node('span', '', product.title), observationLabel(product));
      const button = node('button', '', 'اعرف السبب');
      button.type = 'button';
      button.addEventListener('click', () => openProduct(product));
      row.append(button);
      list.append(row);
    });
    disclosure.append(list);
    return disclosure;
  }

  function render() {
    elements.results.replaceChildren();
    elements.message.replaceChildren();
    const filters = currentFilters();
    renderedStaleness = new Map(products.map((product) => [product.key, productIsStale(product)]));
    updateWatchlistControl();
    const relevant = products.filter((product) => productMatches(product, filters)
      && (!showWatchlistOnly || watchlist.has(product.key)));
    const real = relevant.filter((product) => product.status === 'real' && !productIsStale(product));
    const fresh = relevant.filter((product) => product.status === 'new' && !productIsStale(product));
    const stale = relevant.filter((product) => ['real', 'new'].includes(product.status) && productIsStale(product));
    const fake = relevant.filter((product) => product.status === 'fake');
    const savedOther = showWatchlistOnly ? relevant.filter((product) => ['fake', 'none'].includes(product.status)) : [];
    const visibleCount = real.length + fresh.length + stale.length + savedOther.length;
    elements.count.textContent = loadError ? 'مش قادرين نعرض الأسعار دلوقتي' : `${digits.format(visibleCount)} منتج — ${digits.format(real.length)} خصم حقيقي بقراءة خلال ٦ ساعات، ${digits.format(fresh.length)} جديد، ${digits.format(stale.length)} قراءة أقدم من ٦ ساعات${showWatchlistOnly ? `، ${digits.format(savedOther.length)} محفوظ للمتابعة` : ''}`;

    if (loadError) addNotice('تعذر تحميل ملف الأسعار. مفيش أسعار مخزنة للعرض؛ هنحاول تاني لما تحدّث الصفحة.', 'معلومة:');
    else if (!updatedAt && products.length) addNotice('بيانات الأسعار موجودة لكن وقت آخر تحديث مش واضح، ومش قادرين نضمن إنها حديثة. اتأكد من سعر المتجر قبل الشراء.', 'تاريخ التحديث غير معروف.');
    else if (!updatedAt) addNotice('لسه ملف الأسعار ما وصلش. مش هنعرض أسعار أو خصومات من غير بيانات حقيقية.', 'مفيش بيانات لسه.');
    else if ((Date.now() - updatedAt.getTime()) / 3_600_000 > 6) addNotice('آخر تحديث عدى عليه أكتر من ٦ ساعات. ممكن التحديث يكون متأخر أو بعض صفحات المتاجر ما كانتش متاحة؛ اتأكد من السعر قبل الشراء.', 'البيانات قديمة.');
    if (blockedPages.length) {
      addNotice(`في ${digits.format(blockedPages.length)} صفحة متجر ما كانتش متاحة وقت آخر تشغيل؛ النتائج دي للصفحات اللي اتقرت بس.`, 'تغطية جزئية:');
    }
    if (filters.minDiscount > 0 && products.some((product) => product.status === 'new' && productMatches(product, { ...filters, minDiscount: 0 }))) {
      addNotice('الحد الأدنى للنسبة بيخفي المنتجات الجديدة مؤقتًا، لأنها لسه من غير مقارنة سعرية.', 'ملحوظة للفلاتر:');
    }

    const hasProducts = relevant.some((product) => ['real', 'new', 'fake'].includes(product.status)) || savedOther.length > 0;
    if (!hasProducts) {
      const hasAnyData = products.length > 0;
      const title = showWatchlistOnly && watchlist.size === 0
        ? 'قائمة المتابعة فاضية'
        : showWatchlistOnly || filters.query || filters.store !== 'all' || filters.category !== 'all' || filters.minDiscount > 0
        ? 'ملقيناش حاجة بالمواصفات دي'
        : 'لسه مفيش أسعار مسجلة';
      const message = showWatchlistOnly && watchlist.size === 0
        ? 'افتح تفاصيل أي منتج واضغط «تابع السعر» علشان تحفظه على جهازك.'
        : showWatchlistOnly
        ? filters.query || filters.store !== 'all' || filters.category !== 'all' || filters.minDiscount > 0
          ? 'مفيش منتج محفوظ بيطابق الفلاتر دي. جرّب تغيّر البحث أو الفلاتر.'
          : 'المنتجات المحفوظة مش موجودة ضمن بيانات الأسعار الحالية.'
        : filters.query || filters.store !== 'all' || filters.category !== 'all' || filters.minDiscount > 0
        ? 'جرّب تغيّر البحث أو الفلاتر. مش بنكمّل النتايج بأسعار تخمينية.'
        : hasAnyData ? 'مفيش نزول حقيقي مؤكد أو منتجات جديدة للعرض حاليًا.' : 'أول ما بيانات المنتجات توصل، هتظهر هنا. لحد وقتها مش هنخمن أسعار.';
      elements.results.append(emptyState(title, message));
      elements.clear.hidden = !(filters.query || filters.store !== 'all' || filters.category !== 'all' || filters.minDiscount > 0);
      return;
    }
    elements.clear.hidden = !(filters.query || filters.store !== 'all' || filters.category !== 'all' || filters.minDiscount > 0);

    if (real.length || (!fresh.length && !stale.length && !fake.length)) {
      const realSection = node('section', 'deal-section');
      realSection.setAttribute('aria-label', 'الخصومات الحقيقية بقراءات عمرها أقل من ٦ ساعات');
      realSection.append(sectionHeading('نزول حقيقي — قراءة خلال ٦ ساعات', real.length, 'real'));
      if (real.length) {
        const grid = node('div', 'product-grid');
        real.forEach((product) => grid.append(productCard(product)));
        realSection.append(grid);
      } else {
        realSection.append(emptyState('مفيش نزول حقيقي مؤكد بقراءة حديثة', 'هنعرض العرض هنا لما قراءة سعر عمرها أقل من ٦ ساعات تبقى أقل من سجل المنتج نفسه.'));
      }
      elements.results.append(realSection);
    }

    if (fresh.length) {
      const newSection = node('section', 'deal-section new-section');
      newSection.setAttribute('aria-label', 'منتجات لسه بنتابع أسعارها');
      newSection.append(sectionHeading('لسه بنتابع السعر', fresh.length, 'new'));
      const grid = node('div', 'product-grid');
      fresh.forEach((product) => grid.append(productCard(product)));
      newSection.append(grid);
      elements.results.append(newSection);
    }
    if (stale.length) {
      const staleSection = node('section', 'deal-section stale-section');
      staleSection.setAttribute('aria-label', 'أسعار بآخر رصد قديم');
      staleSection.append(sectionHeading('آخر أسعار مرصودة — قد تكون اتغيّرت', stale.length, 'stale'));
      const grid = node('div', 'product-grid');
      stale.forEach((product) => grid.append(productCard(product)));
      staleSection.append(grid);
      elements.results.append(staleSection);
    }
    if (savedOther.length) {
      const savedSection = node('section', 'deal-section');
      savedSection.setAttribute('aria-label', 'أسعار محفوظة في قائمة المتابعة');
      savedSection.append(sectionHeading('أسعار محفوظة للمتابعة', savedOther.length, 'new'));
      const grid = node('div', 'product-grid');
      savedOther.forEach((product) => grid.append(productCard(product)));
      savedSection.append(grid);
      elements.results.append(savedSection);
    }
    const excluded = renderExcluded(showWatchlistOnly ? [] : fake);
    if (excluded) elements.results.append(excluded);
  }

  function chartFor(product) {
    const valid = product.history;
    if (valid.length < 2) return node('p', 'chart-empty', 'لسه عدد القراءات مش كفاية لرسم تاريخ سعر واضح.');
    const svgNS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(svgNS, 'svg');
    svg.setAttribute('class', 'price-chart');
    svg.setAttribute('viewBox', '0 0 600 210');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', `رسم تاريخ السعر لمنتج ${product.title}`);
    const title = document.createElementNS(svgNS, 'title');
    title.textContent = `تاريخ السعر من ${formatDate(valid[0].date)} إلى ${formatDate(valid[valid.length - 1].date)}`;
    svg.append(title);
    const values = valid.map((point) => point.price);
    const min = Math.min(...values);
    const max = Math.max(...values);
    const spread = max - min || Math.max(max * .08, 1);
    const low = min - spread * .12;
    const high = max + spread * .12;
    const x = (index) => 25 + (index / (valid.length - 1)) * 550;
    const y = (price) => 18 + ((high - price) / (high - low)) * 150;
    [0, 1, 2].forEach((line) => {
      const guide = document.createElementNS(svgNS, 'line');
      const gy = 24 + line * 58;
      guide.setAttribute('x1', '20'); guide.setAttribute('x2', '580'); guide.setAttribute('y1', String(gy)); guide.setAttribute('y2', String(gy));
      guide.setAttribute('stroke', 'var(--line)'); guide.setAttribute('stroke-dasharray', '3 5');
      svg.append(guide);
    });
    const points = valid.map((point, index) => `${x(index)},${y(point.price)}`).join(' ');
    const area = document.createElementNS(svgNS, 'polygon');
    area.setAttribute('points', `25,178 ${points} 575,178`);
    area.setAttribute('fill', 'var(--chart-fill)');
    svg.append(area);
    const line = document.createElementNS(svgNS, 'polyline');
    line.setAttribute('points', points); line.setAttribute('fill', 'none'); line.setAttribute('stroke', 'var(--chart)');
    line.setAttribute('stroke-width', '4'); line.setAttribute('stroke-linecap', 'round'); line.setAttribute('stroke-linejoin', 'round');
    svg.append(line);
    valid.forEach((point, index) => {
      const circle = document.createElementNS(svgNS, 'circle');
      circle.setAttribute('cx', String(x(index))); circle.setAttribute('cy', String(y(point.price)));
      circle.setAttribute('r', index === valid.length - 1 ? '6' : '3.5');
      circle.setAttribute('fill', 'var(--surface)'); circle.setAttribute('stroke', 'var(--chart)'); circle.setAttribute('stroke-width', '3');
      const pointTitle = document.createElementNS(svgNS, 'title');
      pointTitle.textContent = `${formatDate(point.date)}: ${money.format(point.price)}`;
      circle.append(pointTitle); svg.append(circle);
    });
    const labels = [
      { text: formatDate(valid[0].date), x: 25, anchor: 'start' },
      { text: formatDate(valid[valid.length - 1].date), x: 575, anchor: 'end' },
    ];
    labels.forEach((item) => {
      const label = document.createElementNS(svgNS, 'text');
      label.textContent = item.text; label.setAttribute('x', String(item.x)); label.setAttribute('y', '202');
      label.setAttribute('text-anchor', item.anchor); label.setAttribute('fill', 'var(--muted)'); label.setAttribute('font-size', '12');
      svg.append(label);
    });
    return svg;
  }

  function detailStatus(product) {
    const stale = productIsStale(product);
    const status = node('div', `detail-status${product.status === 'new' || stale ? ' is-new' : ''}${product.status === 'fake' ? ' is-fake' : ''}`);
    let title = 'التقييم لسه مش محسوم';
    let text = 'مفيش بيانات كفاية علشان نأكد إذا كان السعر نازل بجد.';
    if (product.status === 'real') {
      title = stale ? 'كان نزولًا حقيقيًا وقت آخر رصد' : 'نزول حقيقي مقارنة بتاريخ السعر';
      text = stale
        ? `وقت آخر رصد، كان السعر المسجل أقل من وسيط آخر ٣٠ يوم بحوالي ${digits.format(product.discount)}٪. السعر ممكن يكون اتغيّر من ساعتها.`
        : `السعر المسجل أقل من وسيط السعر خلال آخر ٣٠ يوم بحوالي ${digits.format(product.discount)}٪.`;
      if (!stale && product.lowestDays !== null) text += product.lowestDays === 0 ? ' وهو أقل سعر مسجل النهارده.' : ` وده أقل سعر اتسجل من ${digits.format(product.lowestDays)} يوم.`;
    } else if (product.status === 'new') {
      title = stale ? 'آخر رصد قديم — مفيش تأكيد للخصم' : 'جديد — بنتابع السعر';
      text = stale
        ? 'وقت آخر رصد كانت البيانات لسه مش كفاية للمقارنة. آخر سعر ممكن يكون اتغيّر ومفيش سعر حالي مؤكد.'
        : 'لسه بنجمع قراءات كفاية من تاريخ المنتج. مش هنحسب خصم قبل ما يبقى فيه مقارنة موثوقة.';
    } else if (product.status === 'fake') {
      title = stale ? 'التقييم وقت آخر رصد: خصم مش حقيقي' : 'خصم وهمي — مش محسوب ضمن العروض';
      text = stale
        ? 'وقت آخر رصد، السعر ما كانش أقل بما يكفي من سجل المنتج أو كان ارتفع قبل الخصم. السعر ممكن يكون اتغيّر من ساعتها.'
        : 'السعر المسجل مش أقل بما يكفي من تاريخ المنتج، أو السعر ارتفع قبل إعلان الخصم. سعر «قبل» وحده مش دليل.';
    }
    status.append(node('span', '', product.status === 'real' && !stale ? '✓' : product.status === 'fake' ? '!' : '◷'));
    const copy = node('p');
    copy.append(node('strong', '', title), document.createTextNode(text));
    status.append(copy);
    return status;
  }

  function renderDetails(product) {
    details.replaceChildren();
    const stale = productIsStale(product);
    const top = node('div', 'detail-top');
    top.append(imageOrFallback(top, product, 'detail-image'));
    const heading = node('div', 'detail-heading');
    const meta = node('div', 'product-meta');
    meta.append(node('span', '', storeLabels[product.store]), node('span', '', categoryLabels[product.category] || 'منتج'));
    const title = node('h2', '', product.title);
    title.id = 'detail-title';
    heading.append(meta, title);
    const price = node('div', 'detail-price');
    price.append(node('strong', '', money.format(product.currentPrice)));
    if (product.status === 'real') price.append(node('span', '', stale ? `كان أقل بـ ${digits.format(product.discount)}٪ وقت الرصد` : `−${digits.format(product.discount)}٪ من وسيط ٣٠ يوم`));
    heading.append(node('span', 'price-label', stale ? 'آخر سعر مرصود — قد يكون اتغيّر' : 'السعر المسجّل'));
    heading.append(price);
    heading.append(observationLabel(product));
    top.append(heading);
    details.append(top, detailStatus(product));

    const chartWrap = node('section', 'chart-wrap');
    const chartHeading = node('div', 'chart-heading');
    chartHeading.append(node('h3', '', 'سجل السعر'), node('span', '', `${digits.format(product.history.length)} قراءة مسجلة`));
    chartWrap.append(chartHeading, chartFor(product), node('p', 'chart-note', 'الرسم مبني على القراءات المتاحة فقط — مش توقع للسعر القادم.'));
    details.append(chartWrap);

    const extra = node('div', 'detail-extra');
    if (product.seller) {
      const seller = node('span'); seller.append(document.createTextNode('البائع: '), node('strong', '', product.seller)); extra.append(seller);
    }
    if (product.listPrice !== null) {
      const list = node('span'); list.append(document.createTextNode('سعر «قبل الخصم» من المتجر: '), node('strong', '', money.format(product.listPrice))); extra.append(list);
    }
    details.append(extra);
    const actions = node('div', 'detail-actions');
    const watching = watchlist.has(product.key);
    const watch = node('button', 'action-link watch-toggle', watching ? '✓ بتابع السعر' : '♡ تابع السعر');
    watch.type = 'button';
    watch.setAttribute('aria-pressed', String(watching));
    const watchFeedback = node('p', 'watchlist-feedback');
    watch.addEventListener('click', () => {
      if (watchlist.has(product.key)) {
        watchlist.delete(product.key);
      } else {
        if (watchlist.size >= 200) {
          watchFeedback.textContent = 'قائمة المتابعة وصلت للحد الأقصى (٢٠٠ منتج).';
          return;
        }
        watchlist.add(product.key);
      }
      saveWatchlist();
      const isWatching = watchlist.has(product.key);
      watch.setAttribute('aria-pressed', String(isWatching));
      watch.textContent = isWatching ? '✓ بتابع السعر' : '♡ تابع السعر';
      watchFeedback.textContent = watchlistStorageFailed
        ? `اتحدّثت القائمة مؤقتًا، لكن مش هتفضل محفوظة بعد إغلاق الصفحة. ${isWatching ? 'المنتج اتضاف.' : 'المنتج اتشال.'}`
        : isWatching ? 'اتحفظ المنتج في قائمة المتابعة على جهازك.' : 'اتشال المنتج من قائمة المتابعة.';
      updateWatchlistControl();
      render();
    });
    actions.append(watch);
    const storeUrl = safeStoreUrl(product);
    if (storeUrl) {
      const shop = node('a', 'action-link primary', 'افتح المنتج في المتجر ↗');
      shop.href = storeUrl.href; shop.target = '_blank'; shop.rel = 'noopener noreferrer'; actions.append(shop);
    }
    const share = node('a', 'action-link whatsapp', 'شارك على واتساب ↗');
    const shareText = `${product.title} — ${money.format(product.currentPrice)} في ${storeLabels[product.store]}\n${window.location.href}`;
    share.href = `https://wa.me/?text=${encodeURIComponent(shareText)}`;
    share.target = '_blank'; share.rel = 'noopener noreferrer'; actions.append(share);
    const telegram = configuredTelegramChannel();
    if (telegram) {
      const channel = node('a', 'action-link telegram-link', 'تابع تنبيهات خَصم العامة على تيليجرام ↗');
      channel.href = telegram.href; channel.target = '_blank'; channel.rel = 'noopener noreferrer'; actions.append(channel);
    }
    details.append(actions, watchFeedback,
      node('p', 'watchlist-note', telegram
        ? 'قائمة المتابعة على هذا المتصفح فقط؛ مش بتوصل للسيرفر ومش بتبعت تنبيهات شخصية. قناة تيليجرام عامة.'
        : 'قائمة المتابعة على هذا المتصفح فقط؛ مش بتوصل للسيرفر ومش بتبعت تنبيهات شخصية. قناة تيليجرام مش مهيأة هنا.'),
      node('p', 'dialog-footnote', 'ده آخر سعر اتسجل، مش ضمان إنه السعر الحالي. راجع سعر المتجر وقت الشراء. تقييمنا مبني على تاريخ السعر، مش على سعر القائمة.'));
  }

  function syncProductRoute() {
    const key = new URLSearchParams(window.location.search).get('product');
    if (!key) {
      if (dialog.open) dialog.close();
      return;
    }
    const product = products.find((item) => item.key === key);
    if (product) openProduct(product, false);
    else if (dialog.open) dialog.close();
  }

  function clearProductQuery() {
    const url = new URL(window.location.href);
    if (!url.searchParams.has('product')) return;
    url.searchParams.delete('product');
    history.replaceState(null, '', url);
  }

  async function loadProducts() {
    try {
      const payload = await fetchProductsPayload();
      if (!payload || typeof payload !== 'object' || !Array.isArray(payload.products)) throw new Error('Invalid products data');
      const stamp = typeof payload.updated_at === 'string' ? Date.parse(payload.updated_at) : NaN;
      updatedAt = Number.isFinite(stamp) ? new Date(stamp) : null;
      products = payload.products.map(normalizeProduct).filter(Boolean);
      const list = Array.isArray(payload.blocked_pages) ? payload.blocked_pages : Array.isArray(payload.scrape_status?.blocked_pages) ? payload.scrape_status.blocked_pages : [];
      const unavailable = Array.isArray(payload.unavailable_pages) ? payload.unavailable_pages : [];
      blockedPages = [...list, ...unavailable].slice(0, 100);
      loadError = false;
    } catch {
      loadError = true;
      products = [];
    }
    updateFreshness();
    render();
    syncProductRoute();
  }

  const themeButton = byId('theme-toggle');
  const themeLabel = byId('theme-label');
  function setTheme(theme, persist = true) {
    const dark = theme === 'dark';
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    themeButton.setAttribute('aria-pressed', String(dark));
    themeLabel.textContent = dark ? 'الوضع الفاتح' : 'الوضع الداكن';
    themeButton.querySelector('.theme-icon').textContent = dark ? '☼' : '☾';
    if (persist) {
      try { localStorage.setItem('khasm-theme', dark ? 'dark' : 'light'); } catch { /* Storage can be unavailable in private browsing. */ }
    }
  }
  let savedTheme = null;
  try { savedTheme = localStorage.getItem('khasm-theme'); } catch { /* Use the browser preference. */ }
  setTheme(savedTheme === 'dark' || savedTheme === 'light' ? savedTheme : (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'), false);
  themeButton.addEventListener('click', () => setTheme(document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark'));

  readWatchlist();
  elements.watchlistToggle.addEventListener('click', () => {
    showWatchlistOnly = !showWatchlistOnly;
    render();
  });
  window.addEventListener('storage', (event) => {
    if (event.key !== watchlistStorageKey && event.key !== null) return;
    readWatchlist();
    render();
    syncProductRoute();
  });
  function refreshClock() {
    refreshObservationLabels();
    updateFreshness();
    if (products.some((product) => productIsStale(product) !== renderedStaleness.get(product.key))) {
      render();
      syncProductRoute();
    }
  }
  window.setInterval(refreshClock, 60_000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) refreshClock();
  });

  ['input', 'change'].forEach((eventName) => {
    elements.search.addEventListener(eventName, render);
    elements.store.addEventListener(eventName, render);
    elements.category.addEventListener(eventName, render);
    elements.discount.addEventListener(eventName, render);
  });
  byId('filters').addEventListener('submit', (event) => event.preventDefault());
  elements.clear.addEventListener('click', () => {
    elements.search.value = ''; elements.store.value = 'all'; elements.category.value = 'all'; elements.discount.value = '0'; render(); elements.search.focus();
  });
  byId('detail-close').addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', clearProductQuery);
  window.addEventListener('popstate', syncProductRoute);
  loadProducts();
})();
