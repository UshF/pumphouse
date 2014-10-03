function API(endpoint) {
    this._request = require('superagent');
    this._endpoint = endpoint;

    this._no_callback = function(res) {};

    this.resources = function() {
        // Returns world's picture
        return this._request.get(this._endpoint + '/resources');
    };

    this.reset = function(callback) {
        // Initiates clouds cleanup and setup operations
        return this._request
            .post(this._endpoint + '/reset')
            .end(function(err, res) {
                (callback || this._no_callback)(err, res);
            });
    };

    this.migrateTenant = function(tenant_id, callback) {
        // Initiates tenant migration
        return this._request
            .post(this._endpoint + '/tenants/' + tenant_id)
            .end(function(err, res) {
                (callback || this._no_callback)(err, res);
            });
    };

    this.evacuateHost = function(host_name, callback) {
        // Initiates host evacuation
        return this._request
            .post(this._endpoint + '/hosts/' + host_name)
            .end(function(err, res) {
                (callback || this._no_callback)(err, res);
            });
    };
};

module.exports = API;

