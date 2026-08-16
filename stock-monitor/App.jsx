import React, { useState, useEffect } from 'react';
import './App.css';

function App() {
  const [profiles, setProfiles] = useState([]);
  const [currentProfile, setCurrentProfile] = useState(null);
  const [profileName, setProfileName] = useState('');
  const [availableStocks, setAvailableStocks] = useState([]);
  const [watchlist, setWatchlist] = useState([]);
  const [signals, setSignals] = useState([]);
  const [searchTerm, setSearchTerm] = useState('');
  const [showNewProfile, setShowNewProfile] = useState(false);
  const [showStockPicker, setShowStockPicker] = useState(false);
  const [loading, setLoading] = useState(false);
  const [selectedStock, setSelectedStock] = useState('');

  const API_URL = process.env.REACT_APP_API_URL || 'http://localhost:8000';

  useEffect(() => {
    loadProfiles();
    loadAvailableStocks();
  }, []);

  useEffect(() => {
    if (currentProfile) {
      loadProfileDetails(currentProfile.id);
      loadSignals(currentProfile.id);
      const interval = setInterval(() => loadSignals(currentProfile.id), 30000);
      return () => clearInterval(interval);
    }
  }, [currentProfile]);

  const loadProfiles = async () => {
    try {
      const response = await fetch(`${API_URL}/api/profiles`);
      const data = await response.json();
      setProfiles(data.profiles);
      if (data.profiles.length > 0) {
        setCurrentProfile(data.profiles[0]);
      }
    } catch (error) {
      console.error('Error loading profiles:', error);
    }
  };

  const loadAvailableStocks = async () => {
    try {
      const response = await fetch(`${API_URL}/api/stocks`);
      const data = await response.json();
      setAvailableStocks(data.stocks);
    } catch (error) {
      console.error('Error loading stocks:', error);
    }
  };

  const loadProfileDetails = async (profileId) => {
    try {
      const response = await fetch(`${API_URL}/api/profiles/${profileId}`);
      const data = await response.json();
      setWatchlist(data.stocks);
    } catch (error) {
      console.error('Error loading profile details:', error);
    }
  };

  const loadSignals = async (profileId) => {
    setLoading(true);
    try {
      const response = await fetch(`${API_URL}/api/signals/${profileId}`);
      const data = await response.json();
      setSignals(data.signals);
    } catch (error) {
      console.error('Error loading signals:', error);
    } finally {
      setLoading(false);
    }
  };

  const createProfile = async () => {
    if (!profileName.trim()) return;
    try {
      const response = await fetch(`${API_URL}/api/profiles`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: profileName, description: '' })
      });
      if (response.ok) {
        setProfileName('');
        setShowNewProfile(false);
        loadProfiles();
      }
    } catch (error) {
      console.error('Error creating profile:', error);
    }
  };

  const addStockToProfile = async () => {
    if (!currentProfile || !selectedStock) return;
    try {
      const response = await fetch(
        `${API_URL}/api/profiles/${currentProfile.id}/stocks?stock_symbol=${selectedStock}`,
        { method: 'POST' }
      );
      if (response.ok) {
        setSelectedStock('');
        setSearchTerm('');
        loadProfileDetails(currentProfile.id);
      }
    } catch (error) {
      console.error('Error adding stock:', error);
    }
  };

  const removeStock = async (stock) => {
    if (!currentProfile) return;
    try {
      await fetch(
        `${API_URL}/api/profiles/${currentProfile.id}/stocks/${stock}`,
        { method: 'DELETE' }
      );
      loadProfileDetails(currentProfile.id);
    } catch (error) {
      console.error('Error removing stock:', error);
    }
  };

  const deleteProfile = async (profileId) => {
    try {
      await fetch(`${API_URL}/api/profiles/${profileId}`, { method: 'DELETE' });
      loadProfiles();
    } catch (error) {
      console.error('Error deleting profile:', error);
    }
  };

  const filteredStocks = availableStocks.filter(stock =>
    stock.toLowerCase().includes(searchTerm.toLowerCase()) &&
    !watchlist.includes(stock)
  );

  const getSignalColor = (signal) => {
    if (signal === 'BUY') return '#10b981';
    if (signal === 'SELL') return '#ef4444';
    return '#f59e0b';
  };

  const getSignalBgColor = (signal) => {
    if (signal === 'BUY') return '#ecfdf5';
    if (signal === 'SELL') return '#fef2f2';
    return '#fffbeb';
  };

  return (
    <div className="container">
      <header className="header">
        <h1>📈 Stock Monitor</h1>
        <p>Real-time signals with Camarilla Pivots, Moving Averages & VWAP</p>
      </header>

      <div className="main-content">
        {/* Profile Management */}
        <div className="sidebar">
          <h2>Profiles</h2>
          <div className="profile-list">
            {profiles.map(profile => (
              <div
                key={profile.id}
                className={`profile-item ${currentProfile?.id === profile.id ? 'active' : ''}`}
              >
                <button
                  className="profile-button"
                  onClick={() => setCurrentProfile(profile)}
                >
                  {profile.name}
                </button>
                {profiles.length > 1 && (
                  <button
                    className="delete-btn"
                    onClick={() => deleteProfile(profile.id)}
                  >
                    ×
                  </button>
                )}
              </div>
            ))}
          </div>

          {!showNewProfile ? (
            <button
              className="btn btn-primary"
              onClick={() => setShowNewProfile(true)}
              style={{ width: '100%', marginTop: '1rem' }}
            >
              + New Profile
            </button>
          ) : (
            <div className="new-profile-form">
              <input
                type="text"
                placeholder="Profile name"
                value={profileName}
                onChange={(e) => setProfileName(e.target.value)}
                className="input"
              />
              <button className="btn btn-success" onClick={createProfile}>Create</button>
              <button className="btn btn-secondary" onClick={() => setShowNewProfile(false)}>Cancel</button>
            </div>
          )}
        </div>

        {/* Main Dashboard */}
        <div className="dashboard">
          {currentProfile ? (
            <>
              <div className="profile-header">
                <h2>{currentProfile.name}</h2>
                <p>Monitoring {watchlist.length}/30 stocks</p>
              </div>

              {/* Stock Selection */}
              {!showStockPicker ? (
                <button
                  className="btn btn-primary"
                  onClick={() => setShowStockPicker(true)}
                  disabled={watchlist.length >= 30}
                >
                  + Add Stocks to Watchlist
                </button>
              ) : (
                <div className="stock-picker">
                  <div className="stock-picker-header">
                    <h3>Select Stocks</h3>
                    <button
                      className="btn btn-secondary"
                      onClick={() => setShowStockPicker(false)}
                    >
                      Done
                    </button>
                  </div>
                  
                  <input
                    type="text"
                    placeholder="Search stocks (e.g., INFY, TCS, SBIN)"
                    className="input"
                    value={searchTerm}
                    onChange={(e) => setSearchTerm(e.target.value)}
                  />

                  <div className="stock-grid">
                    {filteredStocks.map(stock => (
                      <button
                        key={stock}
                        className={`stock-button ${selectedStock === stock ? 'selected' : ''}`}
                        onClick={() => setSelectedStock(stock)}
                      >
                        {stock}
                      </button>
                    ))}
                  </div>

                  <button
                    className="btn btn-success"
                    onClick={addStockToProfile}
                    disabled={!selectedStock}
                  >
                    Add Selected Stock
                  </button>
                </div>
              )}

              {/* Current Watchlist */}
              {watchlist.length > 0 && (
                <div className="watchlist-section">
                  <h3>Your Watchlist ({watchlist.length}/30)</h3>
                  <div className="watchlist-tags">
                    {watchlist.map(stock => (
                      <div key={stock} className="stock-tag">
                        {stock}
                        <button
                          className="remove-tag"
                          onClick={() => removeStock(stock)}
                        >
                          ×
                        </button>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Signals Dashboard */}
              {signals.length > 0 && (
                <div className="signals-section">
                  <div className="signals-header">
                    <h3>Live Signals</h3>
                    <button
                      className="btn btn-secondary btn-small"
                      onClick={() => loadSignals(currentProfile.id)}
                    >
                      🔄 Refresh
                    </button>
                  </div>
                  {loading && <p className="loading">Fetching latest data...</p>}
                  
                  <div className="signals-grid">
                    {signals.map((signal, idx) => (
                      <div
                        key={idx}
                        className="signal-card"
                        style={{ borderLeftColor: getSignalColor(signal.signal) }}
                      >
                        <div className="signal-top">
                          <h4>{signal.symbol}</h4>
                          <span
                            className="signal-badge"
                            style={{
                              backgroundColor: getSignalBgColor(signal.signal),
                              color: getSignalColor(signal.signal)
                            }}
                          >
                            {signal.signal}
                          </span>
                        </div>

                        <div className="signal-body">
                          <div className="metric">
                            <label>Current Price</label>
                            <span className="value">₹{signal.price.toFixed(2)}</span>
                          </div>
                          <div className="metric">
                            <label>Confidence</label>
                            <span className="value">{(signal.confidence * 100).toFixed(0)}%</span>
                          </div>

                          <div className="indicators">
                            <h5>Technical Indicators</h5>
                            <div className="metric-row">
                              <div className="metric">
                                <label>MA20</label>
                                <span>₹{signal.ma_20.toFixed(2)}</span>
                              </div>
                              <div className="metric">
                                <label>MA50</label>
                                <span>₹{signal.ma_50.toFixed(2)}</span>
                              </div>
                            </div>
                            <div className="metric">
                              <label>VWAP</label>
                              <span>₹{signal.vwap.toFixed(2)}</span>
                            </div>

                            <h5 style={{ marginTop: '0.75rem' }}>Camarilla Pivots</h5>
                            <div className="pivots">
                              <div className="pivot-row">
                                <label>R4</label>
                                <span>₹{signal.camarilla.h4.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>R3</label>
                                <span>₹{signal.camarilla.h3.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>R2</label>
                                <span>₹{signal.camarilla.h2.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>R1</label>
                                <span>₹{signal.camarilla.h1.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row pivot-center">
                                <label>Pivot</label>
                                <span>₹{signal.camarilla.pivot.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>S1</label>
                                <span>₹{signal.camarilla.s1.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>S2</label>
                                <span>₹{signal.camarilla.s2.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>S3</label>
                                <span>₹{signal.camarilla.s3.toFixed(2)}</span>
                              </div>
                              <div className="pivot-row">
                                <label>S4</label>
                                <span>₹{signal.camarilla.s4.toFixed(2)}</span>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {signals.length === 0 && !loading && watchlist.length > 0 && (
                <div className="empty-state">
                  <p>Click "Refresh" to load signals for your stocks</p>
                  <button
                    className="btn btn-primary"
                    onClick={() => loadSignals(currentProfile.id)}
                  >
                    Load Signals
                  </button>
                </div>
              )}
            </>
          ) : (
            <div className="empty-state">
              <p>Create a profile to get started</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default App;
