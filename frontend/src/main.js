import { createApp } from 'vue'
import App from './App.vue'
import router from './router.js'
import './styles/main.css'

// Keep the server-rendered profile in the final DOM. It is a real, readable
// part of a profile page and is placed below the interactive graph by the
// server/CSS. Removing it here made the rendered page substantially emptier.

createApp(App).use(router).mount('#app')
