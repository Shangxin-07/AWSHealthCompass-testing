// CloudFront Function (viewer-request): SPA routing for the Compass dashboard.
// App routes (last path segment has no file extension) are served /index.html;
// real files, including /assets/*, pass through so a missing one is a true 404.
function handler(event) {
  var req = event.request;
  var last = req.uri.substring(req.uri.lastIndexOf('/') + 1);
  if (last.indexOf('.') === -1) {
    req.uri = '/index.html';
  }
  return req;
}
