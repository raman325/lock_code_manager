import { LockCodeManagerDashboardStrategy } from './dashboard-strategy';
import './lock-codes-card';
import './lock-codes-card-editor';
import { LockCodeManagerLockSectionStrategy } from './lock-section-strategy';
import './slot-card';
import './slot-card-editor';
import { LockCodeManagerSlotSectionStrategy } from './slot-section-strategy';
import { LockCodeManagerViewStrategy } from './view-strategy';

declare global {
    interface HTMLElementTagNameMap {
        'll-strategy-dashboard-lock-code-manager': LockCodeManagerDashboardStrategy;
        'll-strategy-section-lock-code-manager-lock': LockCodeManagerLockSectionStrategy;
        'll-strategy-section-lock-code-manager-slot': LockCodeManagerSlotSectionStrategy;
        'll-strategy-view-lock-code-manager': LockCodeManagerViewStrategy;
    }
}

customElements.define('ll-strategy-dashboard-lock-code-manager', LockCodeManagerDashboardStrategy);
customElements.define(
    'll-strategy-section-lock-code-manager-lock',
    LockCodeManagerLockSectionStrategy
);
customElements.define(
    'll-strategy-section-lock-code-manager-slot',
    LockCodeManagerSlotSectionStrategy
);
customElements.define('ll-strategy-view-lock-code-manager', LockCodeManagerViewStrategy);
