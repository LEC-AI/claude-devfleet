import React, { useState } from 'react';
import Sidebar from './components/Sidebar';
import Dashboard from './pages/Dashboard';
import Projects from './pages/Projects';
import MissionBoard from './pages/MissionBoard';
import MissionDetail from './pages/MissionDetail';
import LiveAgent from './pages/LiveAgent';
import Reports from './pages/Reports';
import StatusPage from './pages/StatusPage';
import ProjectDetail from './pages/ProjectDetail';
import Integrations from './pages/Integrations';
import FleetConfig from './pages/FleetConfig';
import PromptStudio from './pages/PromptStudio';
import { AuthProvider, useAuth } from './auth';
import Login from './pages/Login';
import Register from './pages/Register';
import Splash from './pages/Splash';

function AppInner() {
  const { user, loading } = useAuth();
  const [page, setPage] = useState('dashboard');
  const [selectedId, setSelectedId] = useState(null);

  const navigate = (pageName, id = null) => {
    setPage(pageName);
    setSelectedId(id);
  };

  if (loading) {
    return (
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center',
                    height: '100vh', background: '#0a0a0a', color: '#555', fontSize: 14 }}>
        Loading…
      </div>
    );
  }

  if (!user) {
    const invite = new URLSearchParams(window.location.search).get('invite');
    if (invite) return <Register navigate={navigate} inviteToken={invite} />;
    if (page === 'register') return <Register navigate={navigate} />;
    return <Login navigate={navigate} />;
  }

  const renderPage = () => {
    switch (page) {
      case 'dashboard':
        return <Dashboard navigate={navigate} />;
      case 'projects':
        return <Projects navigate={navigate} />;
      case 'project':
        return <ProjectDetail id={selectedId} navigate={navigate} />;
      case 'missions':
        return <MissionBoard navigate={navigate} />;
      case 'mission':
        return <MissionDetail id={selectedId} navigate={navigate} />;
      case 'live':
        return <LiveAgent sessionId={selectedId} navigate={navigate} />;
      case 'reports':
        return <Reports navigate={navigate} />;
      case 'integrations':
        return <Integrations navigate={navigate} />;
      case 'status':
        return <StatusPage navigate={navigate} />;
      case 'fleet-config':
        return <FleetConfig navigate={navigate} />;
      case 'prompt-studio':
        return <PromptStudio navigate={navigate} />;
      case 'splash':
        return <Splash navigate={navigate} />;
      case 'login':
        return <Login navigate={navigate} />;
      case 'register':
        return <Register navigate={navigate} />;
      default:
        return <Dashboard navigate={navigate} />;
    }
  };

  return (
    <div className="app-layout">
      <Sidebar activePage={page} navigate={navigate} />
      <main className="main-content">
        {renderPage()}
      </main>
    </div>
  );
}

function App() {
  return <AuthProvider><AppInner /></AuthProvider>;
}

export default App;
