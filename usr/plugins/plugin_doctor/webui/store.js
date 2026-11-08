import { createStore } from '/js/AlpineStore.js';
import { callJsonApi } from '/js/api.js';
import { toastFrontendError, toastFrontendSuccess } from '/components/notifications/notification-store.js';

const endpoint = '/plugins/plugin_doctor/diagnose';
export const store = createStore('pluginDoctor', {
    plugins: [], target: '_convo', toggle: 'unknown', report: '', busy: false,
    async load() {
        try { this.plugins = (await callJsonApi(endpoint,{action:'list'})).plugins.filter(p=>p!=='plugin_doctor'); }
        catch { toastFrontendError('Plugin inventory unavailable. Check host diagnostics.','Plugin Doctor'); }
    },
    async inspect() {
        this.busy = true;
        try {
            const result = await callJsonApi(endpoint,{action:'inspect',target:this.target});
            this.toggle = result.toggle; this.report = JSON.stringify(result.report,null,2);
        } catch { this.toggle='unknown'; toastFrontendError('Inspection failed. Target remains unchanged.','Plugin Doctor'); }
        finally { this.busy=false; }
    },
    async refresh() {
        if (!window.confirm('Refresh the repaired plugin code/cache while leaving it disabled?')) return;
        this.busy=true;
        try {
            const result=await callJsonApi(endpoint,{action:'refresh_disabled',target:this.target,confirmed:true});
            toastFrontendSuccess(result.message,'Plugin Doctor');
        } catch { toastFrontendError('Refresh failed or target is not disabled. No enable was requested.','Plugin Doctor'); }
        finally { this.busy=false; }
    },
});
