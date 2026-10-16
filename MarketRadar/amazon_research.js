// Python inserts data here; the model never writes browser code or selectors.
async page => {
  const config = /*MARKETRADAR_CONFIG*/ {};
  const result = { status: 'no_results', products: [], notes: [] };
  const url = config.action === 'search'
    ? `https://www.amazon.com/s?k=${encodeURIComponent(config.query)}`
    : `https://www.amazon.com/dp/${config.asin}`;
  try {
    const response = await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
    if (response?.status() === 429) return { ...result, status: 'rate_limited', notes: ['Amazon rate limited this request.'] };
    if (!/^https:\/\/(?:www\.)?amazon\.com\//.test(page.url())) {
      return { ...result, status: 'failed', notes: ['Amazon redirected outside amazon.com.'] };
    }
    if (/\/ap\/signin/.test(page.url())) return { ...result, status: 'login_required', notes: ['Amazon asks for manual sign-in.'] };
    const body = await page.locator('body').innerText({ timeout: 10000 });
    if (await page.locator('form[action*="validateCaptcha"]').count()
        || /enter the characters you see|sorry, we just need to make sure|robot check|verify you are human/i.test(body)) {
      return { ...result, status: 'challenge', notes: ['Amazon verification stopped collection. Complete it manually before a new run.'] };
    }
    const selector = config.action === 'search' ? '[data-component-type="s-search-result"]' : '#productTitle';
    await page.locator(selector).first().waitFor({ state: 'attached', timeout: 10000 });
    if (config.action === 'search') {
      result.products = await page.locator(selector).evaluateAll(nodes => {
        const text = (node, selector) => node.querySelector(selector)?.innerText?.trim() || null;
        return nodes.filter(node => /^[A-Z0-9]{10}$/.test(node.dataset.asin) && !/Sponsored/i.test(node.innerText))
          .slice(0, 6).map(node => {
            const labels = [...node.querySelectorAll('[aria-label]')].map(el => el.getAttribute('aria-label'));
            return { asin: node.dataset.asin, title: text(node, 'h2'),
              url: `https://www.amazon.com/dp/${node.dataset.asin}`,
              price: text(node, '.a-price .a-offscreen'),
              rating: labels.find(label => /out of 5 stars/.test(label)) || null,
              review_count: labels.find(label => /^[\d,.Kk]+ ratings?$/.test(label)) || null,
              demand_signal: node.innerText.match(/[^\n]*bought in past month/i)?.[0]?.trim() || null };
          }).filter(product => product.title);
      });
    } else {
      // Scroll once to load the product's public customer-review section.
      const reviewSection = page.locator('#customerReviews, #reviewsMedley, #customer-reviews_feature_div').first();
      if (await reviewSection.count()) {
        await reviewSection.scrollIntoViewIfNeeded({ timeout: 5000 });
        await page.locator('[data-hook="review"], [data-csa-c-slot-id="cm-cr-dp-sign-in-prompt"]').first()
          .waitFor({ state: 'attached', timeout: 6000 }).catch(() => {});
      }
      const product = await page.evaluate(asin => {
        const text = (root, selector, max = 1200) => root.querySelector(selector)?.innerText?.trim().slice(0, max) || null;
        const reviews = [...document.querySelectorAll('[data-hook="review"]')].map(node => ({
          id: node.id, title: text(node, '[data-hook="reviewTitle"], [data-hook="review-title"]', 200),
          body: text(node, '[data-hook="reviewRichContentContainer"], [data-hook="review-body"]'),
          rating: text(node, '[data-hook="review-star-rating"], [data-hook="cmps-review-star-rating"]', 80),
          date: text(node, '[data-hook="review-date"]', 160),
          variant: text(node, '[data-hook="format-strip"]', 200),
          locale: node.querySelector('[lang]')?.getAttribute('lang') || null,
          verified_purchase: Boolean(node.querySelector('[data-hook="avp-badge"]')),
          url: `https://www.amazon.com/gp/customer-reviews/${node.id}`
        })).filter(review => /^[A-Z0-9]+$/.test(review.id) && review.body);
        // Keep critical feedback as well as top reviews; this is a small, biased sample.
        const critical = reviews.filter(review => parseFloat(review.rating) <= 3).slice(0, 4);
        const sampled = [...critical, ...reviews.filter(review => !critical.some(item => item.id === review.id))].slice(0, 12);
        const reviewText = text(document, '#customerReviews, #reviewsMedley, #customer-reviews_feature_div', 20000) || '';
        return { asin, url: `https://www.amazon.com/dp/${asin}`, title: text(document, '#productTitle', 400),
          price: text(document, '#corePrice_feature_div .a-price .a-offscreen, #corePriceDisplay_desktop_feature_div .a-price .a-offscreen', 80),
          rating: text(document, '#acrPopover .a-icon-alt', 80),
          review_count: text(document, '#acrCustomerReviewText', 80),
          demand_signal: text(document, '#social-proofing-faceout-title-tk_bought', 120),
          features: [...document.querySelectorAll('#feature-bullets li')].map(node => node.innerText.trim().slice(0, 700)).filter(Boolean).slice(0, 8),
          reviews: sampled, visible_review_count: reviews.length,
          review_status: reviews.length ? 'live' : /sign in to see customer reviews/i.test(reviewText) ? 'login_required' : 'unavailable' };
      }, config.asin);
      if (product.title) result.products = [product];
      if (!product.reviews.length) result.notes.push(product.review_status === 'login_required'
        ? `${config.asin}: Amazon says sign in to see customer reviews. Sign in manually in the cloud browser, then run new research; features remain available.`
        : `${config.asin}: no readable customer reviews on the product page.`);
    }
    result.status = result.products.length ? 'live' : 'no_results';
  } catch (error) {
    result.status = 'failed';
    result.notes.push(`Amazon collection failed: ${error.name || 'page error'}. No unreadable fields were guessed.`);
  }
  return result;
}
